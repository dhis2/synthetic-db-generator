"""`dbgen stage orgunits`: the org unit hierarchy the spec's levels draw.

The root, then each level of [orgunits] in turn, below the one before it:

* count = n: n org units, spread evenly over the parents, each parent with one at least
* per_parent = dist: as many under each parent as the distribution draws, which may be an
  expression of the parent's traits and its ancestors', such as "district.people / 25_000"
* types = [...]: of each type, per org unit of the level `per` (the parent's by default),
  Poisson(its people / 100,000 times per_100k, times urban_factor where it is urban), or the
  type's count; spread over the parents below it at random, each parent with one at least as
  far as they go. A type's weight and clerks go with its org units.

Then each level's traits, in the order the spec gives them: a distribution, which may name the
traits before it and those of the levels above; or `largest = <trait>, until_share = s`, 1 for
the org units of the largest trait until they hold the share s of its sum, 0 for the others. Every
org unit has the trait weight, its type's weight or 1.

Every draw is keyed by the org unit's code, never by a record, so an org unit is the same in every
build of the spec, whatever area it covers. A build of an area (--orgunit) draws the whole
hierarchy, and keeps the org units under the area's codes, their ancestors, and every org unit of
the levels down to the deepest of the codes: a district's build keeps every region and district,
and the wards and facilities of its district only.

A code is its parent's and a serial, joined by '-' below the third level: QP, QP01, QP0101,
QP0101-01, QP0101-01-001. A serial has the digits of its level's largest, two at least. Names
are made-up words, a type's name ending its org units' names. The UID is a hash of the code.

Writes orgunits.parquet, every org unit drawn, those of the area kept, each column described in its
schema metadata, and orgunits.json, the metadata payload of the kept ones, which bootstrap
imports.
"""

import sys
from collections.abc import Iterable
from functools import partial
from pathlib import Path

import duckdb
from duckdb.func import FunctionNullHandling
from duckdb.sqltypes import VARCHAR

from dbgen import compile as compile_stage
from dbgen import distribution, expr, parquet, provenance, sandbox, sqlfile
from dbgen.config import Build, listed
from dbgen.distribution import quote
from dbgen.errors import InternalError, UsageError
from dbgen.spec import slug
from dbgen.uid import uid

# DHIS2 requires an opening date. A date long before any data keeps every org unit open for all
# of it: tracker ignores it, aggregate data entry and analytics do not.
OPENING_DATE = "1970-01-01"

COLUMNS = parquet.describe(
    "orgunits",
    {
        "uid": "DHIS2 UID, a hash of the code",
        "code": "The parent's code and a serial, the root's from the spec",
        "name": "Made-up name; ends in the org unit's type where its level has types",
        "level": "1 for the root, 2 for the first level below it, and so on",
        "level_name": "Name of the level, from the spec",
        "parent_code": "Code of the parent, null for the root",
        "parent_uid": "UID of the parent, null for the root",
        "path": "Codes of the org unit's ancestors and its own, from the root, joined by /",
        "type": "Type of the org unit, where its level has types",
        "clerks": "Data clerks of the org unit, where its level sets them",
        "traits": (
            "Its traits as JSON: numbers, dates as days since 1970-01-01, 0 or 1; weight is its "
            "type's, 1 without"
        ),
        "kept": (
            "Whether it is of the build's area: the others are drawn too, as a total of a level "
            "above is shared among all its org units (generate)"
        ),
    },
)


def run(build: Build) -> Path:
    effective, parents = compile_stage.inputs(build, "orgunits")
    provenance.forget(build, "orgunits")
    output = build.make_stage_dir("orgunits")
    con = build_orgunits(effective, [output])
    target = output / "orgunits.parquet"
    parquet.copy(
        con,
        "(select * from orgunit order by level, code)",
        target,
        COLUMNS,
        "compression zstd",
    )
    write_payload(con, effective, output / "orgunits.json")
    counts = con.execute(
        "select level_name, count(*) from orgunit where kept group by level, level_name "
        "order by level"
    ).fetchall()
    print(
        f"orgunits: {sum(n for _, n in counts):,} in the build: {listed(dict(counts))}",
        file=sys.stderr,
    )
    provenance.record(build, "orgunits", parents, effective)
    return target


def connect(
    seed: int, directories: Iterable[Path] = (), database: str = ":memory:"
) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection with dbgen's macros, drawing with seed, confined to the files under
    directories."""
    con = duckdb.connect(database)
    con.execute("set variable seed = ?", [seed])
    for statement in sqlfile.script(sqlfile.SQL / "macros.sql"):
        con.execute(statement)
    con.create_function(
        "dhis2_uid",
        _uid_or_null,
        [VARCHAR, VARCHAR],
        VARCHAR,
        null_handling=FunctionNullHandling.SPECIAL,
    )
    sandbox.confine(con, directories)
    return con


def _uid_or_null(namespace: str, key: str | None) -> str | None:
    return None if key is None else uid(namespace, key)


def build_orgunits(effective: dict, directories: Iterable[Path] = ()) -> duckdb.DuckDBPyConnection:
    """A connection holding table orgunit: every org unit the spec draws, with `kept` true for
    those of the build's area. It reads and writes only the files under directories."""
    orgunits = effective["orgunits"]
    seed = orgunits["seed"] if orgunits["seed"] is not None else effective["build"]["seed"]
    con = connect(seed, directories)
    root = orgunits["root"]
    con.execute(
        """
        create table unit as
        select ?::varchar as code, ?::varchar as name, 1 as level, ?::varchar as level_name,
               null::varchar as parent_code, ?::varchar as path, null::varchar as type,
               null::integer as clerks, '{"weight": 1}'::json as traits
        """,
        [root["code"], root["name"], root["name"], root["code"]],
    )
    root_slug = slug(root["name"])
    # scope_<level>: each org unit of the level with its own code, name, uid and traits, and its
    # ancestors', as "<level>.<name>" columns, which expressions name.
    con.execute(
        f"""
        create table scope_1 as
        select code, code as "{root_slug}.code", name as "{root_slug}.name",
               dhis2_uid('ou', code) as "{root_slug}.uid", 1.0::double as "{root_slug}.weight"
        from unit
        """
    )
    scopes = {root_slug}
    for i, level in enumerate(orgunits["levels"]):
        number = i + 2
        rows = _rows(con, level, number, scopes, orgunits)
        con.execute(f"create or replace table rows as {rows}")
        _make_level(con, level, number)
        scopes.add(level["slug"])
        traits = _draw_traits(con, level, scopes)
        struct = ", ".join(f"'{t}': \"{level['slug']}.{t}\"" for t in traits)
        con.execute(
            f"""
            insert into unit
            select code, name, {number}, ?, parent_code, path, type, clerks,
                   to_json({{{struct}}})
            from work
            """,
            [level["name"]],
        )
        own = ", ".join(
            [f'"{level["slug"]}.{c}"' for c in ("code", "name", "uid")]
            + [f'"{level["slug"]}.{t}"' for t in traits]
        )
        parent = f"scope_{number - 1}"
        con.execute(
            f"""
            create table scope_{number} as
            select w.code, p.* exclude (code), {own}
            from work w
            join {parent} p on p.code = w.parent_code
            """
        )
    _keep(con, tuple(effective["codes"]))
    con.execute(
        """
        create table orgunit as
        select dhis2_uid('ou', code) as uid, code, name, level, level_name, parent_code,
               dhis2_uid('ou', parent_code) as parent_uid, path, type, clerks,
               traits::varchar as traits, kept
        from unit
        order by level, code
        """
    )
    check_hierarchy(con)
    return con


def _rows(
    con: duckdb.DuckDBPyConnection, level: dict, number: int, scopes: set[str], orgunits: dict
) -> str:
    """A query of the level's org units as (parent_code, type, serial)."""
    label = f"orgunits.{level['slug']}"
    parent = f"scope_{number - 1}"
    if "count" in level:
        count = level["count"]
        return f"""
            -- The first parents by code get one more where the count does not divide evenly.
            with parents as (
                select code as parent_code, row_number() over (order by code) as r,
                       count(*) over () as parents
                from {parent}
            )
            select parent_code, null::varchar as type, unnest(range(1, n + 1)) as serial
            from (select parent_code,
                         greatest({count} // parents + (r <= {count} % parents)::int, 1) as n
                  from parents)
        """
    if "per_parent" in level:
        draws = f"{label}.per_parent"
        c = distribution.Compiler(
            "p.code", partial(expr.rewrite, scopes=scopes, key="p.code", label=draws)
        )
        n = c.compile(level["per_parent"], draws)
        try:
            con.execute(f"select {n} from {parent} p limit 0")
        except duckdb.Error as e:
            raise UsageError(f"orgunits.level {level['name']}.per_parent: {e}") from e
        return f"""
            select parent_code, null::varchar as type, unnest(range(1, n + 1)) as serial
            from (select p.code as parent_code, greatest(0, round({n}))::bigint as n
                  from {parent} p)
        """
    return _type_rows(con, level, number, scopes, orgunits)


def _type_rows(
    con: duckdb.DuckDBPyConnection, level: dict, number: int, scopes: set[str], orgunits: dict
) -> str:
    """The org units of a level of types: of each type per org unit of level `per`, spread over
    the parents below it, the first in turn so each parent has one, the others at random."""
    label = f"orgunits.{level['slug']}"
    names = [orgunits["root"]["name"], *(lv["name"] for lv in orgunits["levels"])]
    per_slug = slug(level["per"])
    if names.index(level["per"]) + 1 > number - 1:
        raise UsageError(f"orgunits.level {level['name']}.per: must be its parent's or above")
    parent = f"scope_{number - 1}"
    owners = f"scope_{names.index(level['per']) + 1}"
    counts = []
    for t in level["types"]:
        tl = f"{label}.{t['name']}"
        key = "p.code"
        if "count" in t:
            draws = f"{tl}.count"
            c = distribution.Compiler(
                key, partial(expr.rewrite, scopes=scopes, key=key, label=draws)
            )
            n = c.compile(t["count"], draws)
            n = f"greatest(0, round({n}))::bigint"
        else:
            urban = f'coalesce(p."{per_slug}.urban", 0)'
            rate = (
                f'p."{per_slug}.people" / 100000 * {float(t.get("per_100k", 1))!r} '
                f"* case when {urban} >= 0.5 then {float(t.get('urban_factor', 1))!r} else 1 end"
            )
            n = f"rand_poisson({key}, {quote(tl)}, {rate})"
        counts.append(
            f"select {key} as owner, {quote(t['name'])} as type, {n} as n from {owners} p"
        )
    query = " union all ".join(counts)
    try:
        con.execute(f"create or replace table type_count as {query}")
    except duckdb.Error as e:
        raise UsageError(
            f"orgunits.level {level['name']}.types: {e}. Unless each type has a count, a level "
            f"of types needs the trait people of {level['per']}, and may use urban"
        ) from e
    return f"""
        with parents as (
            select "{per_slug}.code" as owner, code as parent_code,
                   row_number() over (partition by "{per_slug}.code" order by code) as k,
                   count(*) over (partition by "{per_slug}.code") as n_parents
            from {parent}
        ),
        units as (
            select owner, type, unnest(range(n)) as i from type_count where n > 0
        ),
        shuffled as (
            select owner, type, i,
                   row_number() over (partition by owner
                       order by hash(owner, type, i, '{label}.order')) as j
            from units
        ),
        placed as (
            select s.owner, s.type, s.i,
                   case when s.j <= p.n_parents then s.j
                        else 1 + (hash(s.owner, s.j, '{label}.parent') % p.n_parents)::bigint
                   end as k
            from shuffled s
            join (select distinct owner, n_parents from parents) p using (owner)
        )
        select p.parent_code, pl.type,
               row_number() over (partition by p.parent_code
                   order by hash(pl.owner, pl.type, pl.i, '{label}.serial')) as serial
        from placed pl
        join parents p on p.owner = pl.owner and p.k = pl.k
    """


def _make_level(con: duckdb.DuckDBPyConnection, level: dict, number: int) -> None:
    """Create table work: the org units of table rows with their code, name, path, type, clerks
    and their ancestors' scope columns."""
    sep = "" if number <= 3 else "-"
    s = level["slug"]
    weight, clerks = "1.0", "null::integer"
    if "clerks" in level:
        clerks = f"{int(level['clerks'])}"
    if "types" in level:
        weight = (
            "case r.type "
            + " ".join(
                f"when {quote(t['name'])} then {float(t.get('weight', 1))!r}"
                for t in level["types"]
            )
            + " end"
        )
        if any("clerks" in t for t in level["types"]):
            clerks = (
                "case r.type "
                + " ".join(
                    f"when {quote(t['name'])} then {int(t.get('clerks', 1))}"
                    for t in level["types"]
                )
                + " end"
            )
    con.execute(
        f"""
        create or replace table work as
        with coded as (
            select r.*, r.parent_code || '{sep}'
                       || lpad(r.serial::varchar,
                               greatest(2, length(max(r.serial) over ()::varchar))::integer, '0')
                       as code
            from rows r
        )
        select
            r.code,
            word('orgunit', r.code) || coalesce(' ' || r.type, '') as name,
            r.parent_code,
            u.path || '/' || r.code as path,
            r.type,
            {clerks} as clerks,
            p.* exclude (code),
            r.code as "{s}.code",
            word('orgunit', r.code) || coalesce(' ' || r.type, '') as "{s}.name",
            dhis2_uid('ou', r.code) as "{s}.uid",
            r.type as "{s}.type",
            ({weight})::double as "{s}.weight"
        from coded r
        join unit u on u.code = r.parent_code
        join scope_{number - 1} p on p.code = r.parent_code
        """
    )


def _draw_traits(con: duckdb.DuckDBPyConnection, level: dict, scopes: set[str]) -> list[str]:
    """Add the level's traits to table work in the spec's order; return their names, weight
    first."""
    s = level["slug"]
    names = ["weight"]
    for trait, spec in level["traits"].items():
        label = f"trait.{s}.{trait}"
        if isinstance(spec, dict) and "largest" in spec:
            by = spec["largest"]
            if by not in names:
                raise UsageError(f"{label}.largest: {by!r} is not a trait of {s} before it")
            share = float(spec["until_share"])
            # Summed as integers, which are exact (see _rows).
            value = f'round("{s}.{by}" * 1000)::hugeint'
            sql = (
                f'case when coalesce(sum({value}) over (order by "{s}.{by}" desc, code '
                "rows between unbounded preceding and 1 preceding), 0) "
                f"< {share!r} * sum({value}) over () then 1 else 0 end"
            )
        else:
            c = distribution.Compiler(
                "code", partial(expr.rewrite, scopes=scopes, key="code", label=label)
            )
            sql = c.compile(spec, label)
        try:
            con.execute(
                f'create or replace table work as select *, ({sql})::double as "{s}.{trait}" '
                "from work"
            )
        except duckdb.Error as e:
            raise UsageError(f"{label}: {e}") from e
        names.append(trait)
    return names


def _keep(con: duckdb.DuckDBPyConnection, codes: tuple[str, ...]) -> None:
    """Mark the org units of the area of codes: under them, above them, and of the levels down
    to the deepest of them. Without codes, all of them."""
    con.execute("alter table unit add column kept boolean default true")
    if not codes:
        return
    found = dict(
        con.execute(
            "select code, level from unit where code in (select unnest(?))", [list(codes)]
        ).fetchall()
    )
    missing = [c for c in codes if c not in found]
    if missing:
        [(sample,)] = con.execute(
            "select string_agg(code, ', ' order by level, code) from "
            "(select code, level from unit order by level, code limit 12)"
        ).fetchall()
        raise UsageError(
            f"--orgunit {', '.join(missing)}: the spec draws no such org unit; its codes start "
            f"{sample}, ..., and dbgen areas lists them"
        )
    depth = max(found.values())
    con.execute(
        f"""
        update unit set kept = level <= {depth} or exists (
            select 1 from unnest(?) t(c)
            where list_contains(string_split(unit.path, '/'), t.c)
        )
        """,
        [list(codes)],
    )


def check_hierarchy(con: duckdb.DuckDBPyConnection) -> None:
    """Fail on a UID or code two org units share, or a kept org unit whose parent is not."""
    [(codes,)] = con.execute(
        "select list(code order by code) from (select code from orgunit group by code "
        "having count(*) > 1)"
    ).fetchall()
    if codes:
        raise InternalError("org units sharing a code: " + ", ".join(codes[:20]))
    [(uids,)] = con.execute(
        "select list(uid order by uid) from (select uid from orgunit group by uid "
        "having count(*) > 1)"
    ).fetchall()
    if uids:
        raise InternalError("org units sharing a UID: " + ", ".join(uids[:20]))
    [(orphans,)] = con.execute(
        """
        select list(code order by code) from orgunit
        where kept and (parent_code not in (select code from orgunit where kept)
                        or parent_code is null and level <> 1)
        """
    ).fetchall()
    if orphans:
        raise InternalError("org units whose parent is missing: " + ", ".join(orphans[:20]))


def write_payload(con: duckdb.DuckDBPyConnection, effective: dict, path: Path) -> None:
    """Write the kept org units and the levels as the metadata payload, on one line.

    json_merge_patch into an empty object drops the null members (RFC 7396), so the root has no
    parent. A short name is at most 50 characters; a longer name keeps it unique by ending in the
    code."""
    levels = [effective["orgunits"]["root"]["name"]] + [
        lv["name"] for lv in effective["orgunits"]["levels"]
    ]
    con.execute("set variable levels = ?", [levels])
    con.execute(
        f"""
        copy (
            select
                (select list(json_merge_patch('{{}}', json_object(
                    'id', uid,
                    'code', code,
                    'name', name,
                    'shortName', case when length(name) <= 50 then name
                                      else left(name, 49 - length(code)) || ' ' || code end,
                    'openingDate', '{OPENING_DATE}',
                    'parent', case when parent_uid is not null
                                   then json_object('id', parent_uid) end
                )) order by level, code) from orgunit where kept) as "organisationUnits",
                (select list(json_object('id', dhis2_uid('oul', level::varchar), 'name', name,
                                         'level', level) order by level)
                 from unnest(getvariable('levels')) with ordinality as t(name, level))
                    as "organisationUnitLevels"
        ) to ? (format json)
        """,
        [str(path)],
    )
