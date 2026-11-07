"""Users copied in SQL from the first user of their kind DHIS2 created, instead of through the API.

The metadata API hashes each user's password (BCrypt) and validates each user, which takes hours
for the users of all facilities. Every user of a kind (users.py) has the same role, groups and
password, so bootstrap imports the first user of each kind by username through the API, and after
the packages copies that user's rows for the others of the kind, the way core replicates a user
(/api/users/{uid}/replica, HibernateUserStore.insertUserCopy):

* userinfo: the template's row with its own id, UID, UUID, username and names. The password hash
  is the template's, since the users of a kind share one password.
* The org units of capture, search and view: the user's own.
* Every other table that holds the template's user id (its role, groups, settings and previous
  passwords): the template's rows with the user's id.

A value a copy would share with its template where DHIS2's schema wants it unique fails the copy
with the constraint Postgres names: a unique userinfo column set in the template, or a table
with an id of its own.

The API imports a clerk, an officer and the admin, and SQL copies the other clerks and officers:
92,905 of the 92,908 users of a build of all facilities.

DHIS2 must not run during the copy: its caches would miss the new users.
"""

import sys
from typing import LiteralString, cast

import duckdb
import psycopg

from dbgen.config import Build
from dbgen.errors import InternalError

# userinfo columns each copy gets its own value in; the rest are copied from the template.
OWN_COLUMNS = {
    "userinfoid": "nextval('hibernate_sequence')",
    "uid": "c.uid",
    "uuid": "gen_random_uuid()",
    "username": "c.username",
    "firstname": "c.firstname",
    "surname": "c.surname",
}
# Org unit links, set from each copy's own org units: table and column of table user_copy.
ORG_UNIT_LINKS = {
    "usermembership": "capture_uid",
    "userteisearchorgunits": "search_uid",
    "userdatavieworgunits": "view_uid",
}


def split(build: Build, payload: dict) -> tuple[dict, list[tuple]]:
    """The payload for the API, and the users to copy.

    The users to copy are every user but the first of its kind by username, as (template UID,
    uid, username, first name, surname, capture, search and view org unit UIDs), ordered by
    username; the template is the first user of the kind. They are left out of the payload and
    of the group members.
    """
    copies = duckdb.execute(
        """
        select first_value(uid) over kind as template_uid,
               uid, username, first_name, surname, capture_uid, search_uid, view_uid
        from read_parquet(?)
        window kind as (partition by kind order by username)
        qualify row_number() over kind > 1
        order by username
        """,
        [str(build.stage_dir("users") / "users.parquet")],
    ).fetchall()
    copied = {c[1] for c in copies}
    api_payload = {
        **payload,
        "users": [u for u in payload["users"] if u["id"] not in copied],
        "userGroupMembers": {
            group: [m for m in members if m["id"] not in copied]
            for group, members in payload["userGroupMembers"].items()
        },
    }
    return api_payload, copies


def copy(cur: psycopg.Cursor, copies: list[tuple]) -> None:
    """Insert the copies of split, from their templates, in the caller's transaction."""
    if not copies:
        return
    templates = sorted({c[0] for c in copies})
    cur.execute("select * from userinfo where uid = any(%s) order by uid", [templates])
    names = [d.name for d in cur.description or []]
    template_rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    missing = sorted(set(templates) - {t["uid"] for t in template_rows})
    if missing:
        raise InternalError(
            f"the template users {missing} are not in userinfo, though the users import "
            "succeeded: either split left them out of the API payload, or DHIS2 did not create "
            "them"
        )
    links = _template_links(cur, template_rows)
    try:
        _insert(cur, copies, links)
    except psycopg.errors.UniqueViolation as e:
        raise InternalError(
            f"a copy of a template user breaks {e.diag.constraint_name} of {e.diag.table_name}: "
            f"{e.diag.message_detail} user_copies.copy gives a copy its own value only in "
            f"{', '.join(OWN_COLUMNS)} of userinfo, and its own user id in the other tables"
        ) from e
    print(
        f"bootstrap: copied {len(copies)} users from the first of their kind "
        f"({', '.join(templates)}), with {', '.join(sorted(links))}",
        file=sys.stderr,
    )


def _template_links(cur: psycopg.Cursor, templates: list[dict]) -> dict[str, str]:
    """{table: user column} of every table besides the org unit links holding a template, given
    the templates' userinfo rows.

    The tables are read from the catalog because Postgres would accept copies that miss one: a
    foreign key requires the user a row references, not a row for each user, and DHIS2 requires
    a user's role in the API's validation, which the copy skips. A copy without its
    userrolemembers row would be valid to Postgres, and have no role.

    Fails on a table whose rows would not be copied correctly, which Postgres would accept as
    well: one whose user column is not its only reference to the user (an owner and a last
    updater: the copy's row would keep the template in the other), or one referencing another
    userinfo column than the id (potentialduplicate.lastupdatebyusername references the
    username).
    """
    # Each foreign key column to userinfo, paired with the userinfo column it references.
    cur.execute(
        """
        select distinct c.conrelid::regclass::text, a.attname, r.attname
        from pg_constraint c
        cross join lateral unnest(c.conkey, c.confkey) as k(key, referenced)
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.key
        join pg_attribute r on r.attrelid = c.confrelid and r.attnum = k.referenced
        where c.contype = 'f' and c.confrelid = 'userinfo'::regclass
          and c.conrelid <> 'userinfo'::regclass
        order by 1, 2
        """
    )
    references = cur.fetchall()
    links: dict[str, str] = {}
    for table, column, referenced in references:
        if table in ORG_UNIT_LINKS:
            continue
        values = [t[referenced] for t in templates if t[referenced] is not None]
        if not values or not _scalar(
            cur, f"select exists (select 1 from {table} where {column} = any(%s))", [values]
        ):
            continue
        if referenced != "userinfoid":
            raise InternalError(
                f"{table}.{column} refers to a template user by its {referenced}, which "
                "user_copies.copy does not copy yet"
            )
        if table in links:
            raise InternalError(
                f"{table} refers to a template user twice, which user_copies.copy does not copy yet"
            )
        links[table] = column
    return links


def _insert(cur: psycopg.Cursor, copies: list[tuple], links: dict[str, str]) -> None:
    """Insert the copies into userinfo, the org unit links and the linked tables."""
    cur.execute(
        """
        create temp table user_copy (
            template_uid text, uid text, username text, firstname text, surname text,
            capture_uid text, search_uid text, view_uid text
        ) on commit drop
        """
    )
    with cur.copy("copy user_copy from stdin") as rows:
        for row in copies:
            rows.write_row(row)

    columns = _columns(cur, "userinfo")
    select = ", ".join(OWN_COLUMNS.get(c, f"t.{c}") for c in columns)
    _execute(
        cur,
        f"insert into userinfo ({', '.join(columns)}) "
        f"select {select} from user_copy c join userinfo t on t.uid = c.template_uid "
        "order by c.username",
    )
    # Autovacuum is off in the build and never analyzes a temporary table, so the planner would
    # take userinfo for the few users DHIS2 created, and join the copies to it in a nested loop,
    # quadratic in the users. ANALYZE counts the rows of its own transaction.
    _execute(cur, "analyze user_copy, userinfo")
    for table, column in ORG_UNIT_LINKS.items():
        _execute(
            cur,
            f"insert into {table} (userinfoid, organisationunitid) "
            "select u.userinfoid, o.organisationunitid from user_copy c "
            "join userinfo u on u.uid = c.uid "
            f"join organisationunit o on o.uid = c.{column}",
        )
    for table, column in links.items():
        columns = _columns(cur, table)
        select = ", ".join("u.userinfoid" if c == column else f"l.{c}" for c in columns)
        _execute(
            cur,
            f"insert into {table} ({', '.join(columns)}) "
            f"select {select} from user_copy c "
            "join userinfo t on t.uid = c.template_uid "
            f"join {table} l on l.{column} = t.userinfoid "
            "join userinfo u on u.uid = c.uid",
        )


def _columns(cur: psycopg.Cursor, table: str) -> list[str]:
    cur.execute(
        """
        select column_name from information_schema.columns
        where table_schema = 'public' and table_name = %s and is_generated = 'NEVER'
        order by ordinal_position
        """,
        [table],
    )
    return [name for (name,) in cur.fetchall()]


def _scalar(cur: psycopg.Cursor, statement: str, params: list | None = None):
    """The first column of the first row, or None."""
    _execute(cur, statement, params)
    row = cur.fetchone()
    return row[0] if row else None


def _execute(cur: psycopg.Cursor, statement: str, params: list | None = None) -> None:
    """Run a statement built from our own names or from names the catalog returned.

    psycopg types execute() to take only literal strings, which keeps outside input out of SQL.
    These statements hold none, so they are passed on as they are.
    """
    cur.execute(cast(LiteralString, statement), params)
