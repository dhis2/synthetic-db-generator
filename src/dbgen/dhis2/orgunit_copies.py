"""Org units copied in SQL from one of their level DHIS2 created, instead of through the API.

The metadata API takes the org units in one atomic import, which for 250,000 org units outlasts
the client's timeout of 10 minutes with DHIS2's default heap, and commits in 5 minutes with a heap
of 13 GiB. An org unit of the build differs from another of its level only in the values of its
own, so bootstrap imports a few through the API, and after the packages and the users copies the
row of one of each level for the others, as user_copies.py does the users:

* The API imports the levels; the root; the first org unit of each level by code; the org units
  of the users the API imports, which they must exist for; and the ancestors of these.
* organisationunit: the row of the first org unit of the copy's level the API imported, with the
  copy's own id, UID, code, names, opening date, parent, path and level, the values the payload
  and the hierarchy set (OWN_COLUMNS). A key of the payload the copy does not set fails it.

DHIS2 writes no other row for an org unit it imports, and the copy fails on a table besides the
links of users and programs that holds a template, which a DHIS2 that does would have. A value a
copy would share with its template where DHIS2's schema wants it unique fails the copy with the
constraint Postgres names.

The API checks what the copy does not, such as the lengths of the names: the orgunits stage
writes them as the API takes them.

The API imports at most 4 org units of each level, and SQL copies the others: 250,252 of the
250,256 of the platform-perf example, in 16 s. The programs are assigned to their org units in SQL
too, after the copy.

DHIS2 must not run during the copy: its caches would miss the new org units.
"""

import sys

import psycopg

from dbgen.dhis2.user_copies import _columns, _execute, _scalar
from dbgen.errors import InternalError

# The keys of an org unit of orgunits.json (orgunits.write_payload) the copy sets.
PAYLOAD_KEYS = {"id", "code", "name", "shortName", "openingDate", "parent"}
# organisationunit columns each copy gets its own value in, from table orgunit_copy c, its parent
# among the copies p and its parent among the org units the API imported a; the rest are copied
# from the template t.
OWN_COLUMNS = {
    "organisationunitid": "c.organisationunitid",
    "uid": "c.uid",
    "code": "c.code",
    "name": "c.name",
    "shortname": "c.shortname",
    "openingdate": "c.openingdate",
    "parentid": "coalesce(p.organisationunitid, a.organisationunitid)",
    "path": "c.path",
    "hierarchylevel": "c.hierarchylevel",
}
# Tables that may hold a template's id without being copied: the org units of the users the API
# imports, and the parent of an org unit.
LINKS = {
    ("organisationunit", "parentid"),
    ("usermembership", "organisationunitid"),
    ("userteisearchorgunits", "organisationunitid"),
    ("userdatavieworgunits", "organisationunitid"),
}
# The org units of a user of users.json.
USER_ORG_UNITS = ("organisationUnits", "teiSearchOrganisationUnits", "dataViewOrganisationUnits")


def split(payload: dict, users: list[dict]) -> tuple[dict, list[tuple]]:
    """The payload for the API, and the org units to copy, given the users the API imports.

    The org units to copy are those the API does not import, as (template UID, uid, code, name,
    short name, opening date, parent UID, path, level), ordered by level and code as the payload
    is; the template is the first org unit of the level the API imports.
    """
    units = payload["organisationUnits"]
    unknown = sorted({key for unit in units for key in unit} - PAYLOAD_KEYS)
    if unknown:
        raise InternalError(
            f"org units of orgunits.json have {', '.join(unknown)}, which "
            "orgunit_copies.split does not copy yet"
        )
    paths: dict[str, str] = {}
    levels: dict[str, int] = {}
    first: dict[int, str] = {}
    for unit in units:
        parent = unit.get("parent", {}).get("id")
        if parent is not None and parent not in paths:
            raise InternalError(
                f"org unit {unit['id']} of orgunits.json comes before its parent {parent}"
            )
        paths[unit["id"]] = f"{paths.get(parent, '')}/{unit['id']}"
        levels[unit["id"]] = levels[parent] + 1 if parent else 1
        first.setdefault(levels[unit["id"]], unit["id"])
    needed = {ou["id"] for user in users for key in USER_ORG_UNITS for ou in user.get(key, [])}
    imported = {
        ancestor
        for uid in needed.union(first.values()) & paths.keys()
        for ancestor in paths[uid].split("/")[1:]
    }
    templates: dict[int, str] = {}
    for unit in units:
        if unit["id"] in imported:
            templates.setdefault(levels[unit["id"]], unit["id"])
    copies = [
        (
            templates[levels[unit["id"]]],
            unit["id"],
            unit["code"],
            unit["name"],
            unit["shortName"],
            unit["openingDate"],
            unit.get("parent", {}).get("id"),
            paths[unit["id"]],
            levels[unit["id"]],
        )
        for unit in units
        if unit["id"] not in imported
    ]
    api_payload = {**payload, "organisationUnits": [u for u in units if u["id"] in imported]}
    return api_payload, copies


def copy(cur: psycopg.Cursor, copies: list[tuple]) -> None:
    """Insert the copies of split, from their templates, in the caller's transaction."""
    if not copies:
        return
    templates = sorted({c[0] for c in copies})
    cur.execute(
        "select uid, organisationunitid from organisationunit where uid = any(%s)", [templates]
    )
    ids = dict(cur.fetchall())
    missing = sorted(set(templates) - ids.keys())
    if missing:
        raise InternalError(
            f"the template org units {missing} are not in organisationunit, though the org units "
            "import succeeded: either split left them out of the API payload, or DHIS2 did not "
            "create them"
        )
    _check_links(cur, list(ids.values()))
    try:
        _insert(cur, copies)
    except psycopg.errors.UniqueViolation as e:
        raise InternalError(
            f"a copy of a template org unit breaks {e.diag.constraint_name} of "
            f"{e.diag.table_name}: {e.diag.message_detail} orgunit_copies.copy gives a copy its "
            f"own value only in {', '.join(OWN_COLUMNS)} of organisationunit"
        ) from e
    print(
        f"bootstrap: copied {len(copies)} org units from the first of their level "
        f"({', '.join(templates)})",
        file=sys.stderr,
    )


def _check_links(cur: psycopg.Cursor, templates: list[int]) -> None:
    """Fail on a table besides LINKS holding a template, given the templates' ids: a row DHIS2
    writes for each org unit it imports, which the copies would miss, though Postgres accepts
    them without."""
    cur.execute(
        """
        select distinct c.conrelid::regclass::text, a.attname
        from pg_constraint c
        cross join lateral unnest(c.conkey) as k(key)
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.key
        where c.contype = 'f' and c.confrelid = 'organisationunit'::regclass
        order by 1, 2
        """
    )
    for table, column in cur.fetchall():
        if (table, column) in LINKS:
            continue
        if _scalar(
            cur, f"select exists (select 1 from {table} where {column} = any(%s))", [templates]
        ):
            raise InternalError(
                f"{table}.{column} refers to a template org unit, which orgunit_copies.copy "
                "does not copy yet"
            )


def _insert(cur: psycopg.Cursor, copies: list[tuple]) -> None:
    """Insert the copies into organisationunit, each after its parent."""
    # The ids are drawn as the rows arrive, so in the order of split, parents first.
    cur.execute(
        """
        create temp table orgunit_copy (
            organisationunitid bigint default nextval('hibernate_sequence'),
            template_uid text, uid text, code text, name text, shortname text, openingdate date,
            parent_uid text, path text, hierarchylevel integer
        ) on commit drop
        """
    )
    with cur.copy(
        "copy orgunit_copy (template_uid, uid, code, name, shortname, openingdate, parent_uid, "
        "path, hierarchylevel) from stdin"
    ) as rows:
        for row in copies:
            rows.write_row(row)
    # As in user_copies: without statistics the planner would take both tables for a few rows
    # and join them in nested loops, quadratic in the org units.
    _execute(cur, "analyze orgunit_copy, organisationunit")
    columns = _columns(cur, "organisationunit")
    select = ", ".join(OWN_COLUMNS.get(c, f"t.{c}") for c in columns)
    _execute(
        cur,
        f"insert into organisationunit ({', '.join(columns)}) "
        f"select {select} from orgunit_copy c "
        "join organisationunit t on t.uid = c.template_uid "
        "left join orgunit_copy p on p.uid = c.parent_uid "
        "left join organisationunit a on a.uid = c.parent_uid "
        "order by c.organisationunitid",
    )
    # For the joins on organisationunit after the copy, those of the programs and the users.
    _execute(cur, "analyze organisationunit")
