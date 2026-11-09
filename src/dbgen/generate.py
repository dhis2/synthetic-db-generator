"""`dbgen stage generate`: the records the spec draws, as DHIS2's tracker tables, checked.

generate needs no DHIS2. It writes DHIS2's tracker tables but for two kinds of column, which load
derives while it copies (dhis2/load.py): the metadata, which they reference by UID in a column
<name>_uid, and the data values, a list of {k: data element UID, v: value} until load builds
eventdatavalues. A program's records, in the order generate draws them:

1. places: the org units of the program's level, each with the traits of its own level and of the
   levels above it, as the columns "<level>.<trait>" its expressions name
2. tracked entities: Poisson(per_orgunit times `by`) at each place, or its share by `by` of the
   total of its org unit of level `among`, each with its traits. A program linked from another
   (`from`) adds one for each of the source's enrollments of `where`, at `share`, at the source's
   place, with the source's columns as the scope its `as` names; it runs after its source's
   records
3. enrollments: the count the spec draws per tracked entity, one after another, each with its
   traits and the gap before the next
4. events, relative to their enrollment: the visits, each of the visit stages on one day, and the
   other stages, each at its offset (from the start, or from the end of the visits) or repeating
5. dates: an enrollment's span is its last event's offset; the first enrollment of a tracked
   entity starts on a day of the window, uniform, each next one after the span and gap of the one
   before. Enrollments from the window's end on
   are left out, and events from it on, but the next event of an enrollment still active, which
   is scheduled
6. statuses: an enrollment that runs past the window's end is ACTIVE; the last enrollment of a
   tracked entity that ends within it takes the status the spec draws; earlier ones COMPLETED
   (or CANCELLED, at its share among the two). An event takes its visit's or stage's status;
   SCHEDULE and SKIPPED events carry no values
7. values: each data element of an event's stage, if its condition holds, at its fill rate,
   from its generator
8. attribute values of each tracked entity, of its program and its tracked entity type
9. times and users: an event is created in working hours on its day by a clerk of its org unit,
   an enrollment 5 minutes before its first event, a tracked entity a minute before its first
   enrollment, a relationship of a linked tracked entity minutes after the later of its two;
   updates and deletions at the shares of [records], follow-ups at each program's
10. the tables (sql/tables.sql): ids by creation time, tracker UIDs from them, and the rows
    updated after their creation, which load updates in that order
11. the pre-load checks (sql/checks/preload.sql) against the metadata files' tables, which
    compile writes; any violation fails, naming the check

Every draw is a hash of the seed, the record's key and the field's label (sql/macros.sql), so the
same spec, seed and DuckDB give the same rows, whatever the area: a record's key derives from its
org unit's code. Durations are whole days, so their sums are exact, as a sum of doubles in parallel
is not.

Writes <table>.parquet of each table of TABLES, each column described in its schema metadata,
updated.parquet, and checksums.json, the row count and row digest (digest.py) of each table and of
the org units and users it read: two builds of one spec, seed and DuckDB give the same digests.
"""

import difflib
import json
import math
import re
import shutil
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

import duckdb

from dbgen import compile as compile_stage
from dbgen import digest, distribution, expr, parquet, provenance, sandbox, sqlfile, timing, uid
from dbgen.config import Build, listed
from dbgen.distribution import days, quote
from dbgen.errors import InternalError, UsageError
from dbgen.spec import SCOPES, slug, table_suffix, window

DUCKDB_MEMORY = "8GB"
DUCKDB_THREADS = 6
# Disk left free for the database file, the Parquet output and the rest of the machine.
TEMP_HEADROOM = 10 * 2**30

# Statuses an event with values has.
WITH_VALUES = ("COMPLETED", "ACTIVE")
# The country code of phone numbers: ITU keeps 280 to 289 spare, so no real number has it.
PHONE_COUNTRY_CODE = "+289"

TABLES = [
    "trackedentity",
    "trackedentityattributevalue",
    "enrollment",
    "trackedentityprogramowner",
    "event",
    "relationship",
    "relationshipitem",
]

USER = "UID of the clerk who {}, whose snapshot load writes to {}"
COLUMNS = {
    "trackedentity": {
        "trackedentityid": "Internal ID, in the order of creation",
        "uid": "UID of the tracked entity, a bijection of its ID",
        "created": "A minute before its first enrollment was created",
        "lastupdated": "Its last update, at least its enrollments' and events'",
        "inactive": "Always false",
        "deleted": "Whether it is soft deleted ([records].deleted)",
        "lastsynchronized": "Always 1970-01-01, never synchronized",
        "organisationunit_uid": "UID of the org unit that registered it",
        "trackedentitytype_uid": "UID of its tracked entity type",
        "createdby_uid": USER.format("registered it", "createdbyuserinfo"),
        "lastupdatedby_uid": USER.format("registered it", "lastupdatedbyuserinfo"),
        "potentialduplicate": "Always false",
    },
    "trackedentityattributevalue": {
        "trackedentityid": "Internal ID of the tracked entity",
        "trackedentityattribute_uid": "UID of the attribute",
        "created": "When its tracked entity was created",
        "lastupdated": "When it was last set",
        "value": "The value, as DHIS2 stores it",
        "storedby": "Username of the clerk who last set it",
    },
    "enrollment": {
        "enrollmentid": "Internal ID, in the order of creation",
        "uid": "UID of the enrollment, a bijection of its ID",
        "created": "Five minutes before its first event was created",
        "lastupdated": "Its last update, at least its events' and its completion",
        "occurreddate": "Its enrollment date",
        "enrollmentdate": "Day of its first event",
        "completeddate": "When it was completed, null unless COMPLETED",
        "followup": "Whether it is marked for follow-up, at its program's share (followup)",
        "completedby": "Username of the clerk who completed it",
        "deleted": "Whether it is soft deleted, as its tracked entity",
        "status": "ACTIVE, COMPLETED or CANCELLED",
        "trackedentityid": "Internal ID of its tracked entity",
        "program_uid": "UID of its program",
        "organisationunit_uid": "UID of its org unit",
        "createdby_uid": USER.format("enrolled it", "createdbyuserinfo"),
        "lastupdatedby_uid": USER.format("enrolled it", "lastupdatedbyuserinfo"),
    },
    "trackedentityprogramowner": {
        "trackedentityprogramownerid": "Internal ID, by tracked entity and program",
        "trackedentityid": "Internal ID of the tracked entity",
        "program_uid": "UID of the program",
        "created": "When its first enrollment in the program was created",
        "lastupdated": "When it was created",
        "organisationunit_uid": "UID of the org unit of that first enrollment",
        "createdby": "Username of the clerk who created that first enrollment",
    },
    "event": {
        "eventid": "Internal ID, in the order of creation",
        "uid": "UID of the event, a bijection of its ID",
        "created": "In working hours of its day, or of the day it was scheduled",
        "lastupdated": "Up to 30 minutes after its creation, or its later update",
        "enrollmentid": "Internal ID of its enrollment",
        "programstage_uid": "UID of its program stage",
        "attributeoptioncombo_uid": "UID of its attribute option combo, null for the default",
        "deleted": "Whether it is soft deleted, as its tracked entity",
        "scheduleddate": "The day it was scheduled for",
        "occurreddate": "The day it occurred, null when SCHEDULE or SKIPPED",
        "organisationunit_uid": "UID of its org unit",
        "status": "ACTIVE, COMPLETED, SCHEDULE or SKIPPED",
        "completedby": "Username of the clerk who completed it",
        "completeddate": "When it was completed, null unless COMPLETED",
        "datavalues": (
            "Its data values as a list of {k: data element UID, v: value}; load turns them into "
            "eventdatavalues (sql/event_json.sql)"
        ),
        "createdby_uid": USER.format("created it", "createdbyuserinfo"),
        "lastupdatedby_uid": USER.format("created it", "lastupdatedbyuserinfo"),
        "lastsynchronized": "Always 1970-01-01, never synchronized",
        "assigneduser_uid": (
            "UID of the clerk it is assigned to, at its stage's share (assigned), or null"
        ),
    },
    "relationship": {
        "relationshipid": "Internal ID, in the order of creation",
        "uid": "UID of the relationship, a bijection of its ID",
        "created": "Minutes after the later of its tracked entities was created",
        "lastupdated": "When it was created",
        "lastupdatedby_uid": "UID of the clerk who linked them",
        "relationshiptype_uid": "UID of its relationship type",
        "from_relationshipitemid": "Internal ID of its from item: 2 relationshipid - 1",
        "to_relationshipitemid": "Internal ID of its to item: 2 relationshipid",
        "key": "UIDs of the relationship type, the from and the to, joined by _",
        "inverted_key": "UIDs of the relationship type, the to and the from, joined by _",
        "deleted": "Whether it is soft deleted, as one of its tracked entities is",
    },
    "relationshipitem": {
        "relationshipitemid": "Internal ID: 2 relationshipid - 1 for from, 2 relationshipid for to",
        "relationshipid": "Internal ID of its relationship",
        "trackedentityid": "Internal ID of its tracked entity",
    },
}
UPDATED_COLUMNS = {
    "tbl": "The table of the row",
    "id": "Internal ID of the row",
    "lastupdated": "When it was updated, after its creation",
}


def run(build: Build) -> dict[str, int]:
    effective, parents = compile_stage.inputs(build, "generate")
    provenance.forget(build, "generate")
    output = build.make_stage_dir("generate")
    statements = (build.stage_dir("compile") / "generate.sql").read_text()
    with timing.timed(build.dir, "generate") as t:
        con = _connect(build, output, effective)
        load_inputs(con, build)
        for statement in split(statements):
            try:
                con.execute(statement)
            except duckdb.Error as e:
                raise InternalError(f"generate: {e}\nin:\n{statement[:3000]}") from e
        check_keys(con)
        tables(con, build, effective)
        run_checks(con, sqlfile.checks(sqlfile.SQL / "checks" / "preload.sql"))
        counts = {}
        for table in TABLES:
            # DuckDB reads row groups in parallel and recommends 100k to 1M rows in each.
            counts[table] = parquet.copy(
                con,
                table,
                output / f"{table}.parquet",
                parquet.describe("sql/tables.sql", COLUMNS[table]),
                "compression zstd, row_group_size 100000",
            )
        parquet.copy(
            con,
            "(select * from updated order by lastupdated, tbl, id)",
            output / "updated.parquet",
            parquet.describe("sql/tables.sql", UPDATED_COLUMNS),
        )
        sums = {
            stage: digest.row_digest(
                con, "from read_parquet(?)", [str(build.stage_dir(stage) / f"{stage}.parquet")]
            )
            for stage in ("orgunits", "users")
        } | {table: digest.row_digest(con, f"select * from {table}") for table in TABLES}
        (output / "checksums.json").write_text(json.dumps(sums, indent=2) + "\n")
        con.close()
        (output / "generate.duckdb").unlink()
        t.add(**{f"rows_{k}": v for k, v in counts.items()})
    print(f"generate: rows {listed(counts)}", file=sys.stderr)
    provenance.record(build, "generate", parents, effective)
    return counts


def _connect(build: Build, output: Path, effective: dict) -> duckdb.DuckDBPyConnection:
    database = output / "generate.duckdb"
    database.unlink(missing_ok=True)
    database.with_name(database.name + ".wal").unlink(missing_ok=True)
    con = duckdb.connect(str(database))
    con.execute(f"set memory_limit = '{DUCKDB_MEMORY}'")
    con.execute(f"set threads = {DUCKDB_THREADS}")
    con.execute("set temp_directory = ?", [str(output / "duckdb.tmp")])
    spill = (shutil.disk_usage(output).free - TEMP_HEADROOM) * 2 // 3
    con.execute(f"set max_temp_directory_size = '{max(spill, 0) // 2**20}MiB'")
    inputs = [build.stage_dir(s) for s in ("compile", "orgunits", "users")]
    sandbox.confine(con, [*inputs, output])
    prepare(con, effective)
    return con


def prepare(con: duckdb.DuckDBPyConnection, effective: dict) -> None:
    """The macros and variables the statements read."""
    start, end, now = window(effective)
    con.execute("set variable seed = ?", [effective["build"]["seed"]])
    for name, value in (("window_start", start), ("window_end", end), ("now", now)):
        con.execute(f"set variable {name} = ?", [float(days(value))])
    for statement in sqlfile.script(sqlfile.SQL / "macros.sql"):
        con.execute(statement)


def load_inputs(con: duckdb.DuckDBPyConnection, build: Build) -> None:
    """Tables orgunit and clerk, from the outputs of orgunits and users."""
    con.execute(
        "create or replace table orgunit as select * from read_parquet(?)",
        [str(build.stage_dir("orgunits") / "orgunits.parquet")],
    )
    con.execute(
        """
        create or replace table clerk as
        select username, orgunit_uid,
               row_number() over (partition by orgunit_uid order by username) - 1 as n,
               count(*) over (partition by orgunit_uid) as n_clerks
        from read_parquet(?)
        where kind = 'clerk'
        """,
        [str(build.stage_dir("users") / "users.parquet")],
    )


def tables(con: duckdb.DuckDBPyConnection, build: Build, effective: dict) -> None:
    """DHIS2's tables of the records (sql/tables.sql), and what the pre-load checks read: the
    metadata tables compile wrote, the users, and every event's values as event_value."""
    for macro in uid.SQL_MACROS:
        con.execute(macro)
    start, end, now = window(effective)
    con.execute("set variable start_date = ?", [start])
    con.execute("set variable end_date = ?", [end])
    # The end of the day `now`: a record may be created during it.
    con.execute("set variable now_ts = ?", [datetime.combine(now, time.max)])
    for path in sorted((build.stage_dir("compile") / "metadata").glob("*.parquet")):
        con.execute(
            f"create or replace table meta_{path.stem} as select * from read_parquet(?)",
            [str(path)],
        )
    con.execute(
        "create or replace table user_ref as select uid, username from read_parquet(?)",
        [str(build.stage_dir("users") / "users.parquet")],
    )
    for statement in sqlfile.script(sqlfile.SQL / "values.sql"):
        con.execute(statement)
    for statement in sqlfile.script(sqlfile.SQL / "tables.sql"):
        try:
            con.execute(statement)
        except duckdb.Error as e:
            raise InternalError(f"generate: {e}\nin: {statement[:1500]}") from e
    con.execute(
        "create or replace view event_value as "
        "select eventid, v.k as de_uid, v.v as value "
        "from (select eventid, unnest(datavalues) as v from event)"
    )


def run_checks(con: duckdb.DuckDBPyConnection, checks: dict[str, str]) -> None:
    failures = []
    for name, query in checks.items():
        rows = con.execute(f"select * from ({query}) limit 3").fetchall()
        if rows:
            [(count,)] = con.execute(f"select count(*) from ({query})").fetchall()
            failures.append(f"{name}: {count} row(s), e.g. {rows}")
    if failures:
        raise InternalError(
            f"the generated tables fail {len(failures)} pre-load check(s):\n  "
            + "\n  ".join(failures)
        )
    print(f"generate: {len(checks)} pre-load checks passed", file=sys.stderr)


def empty_inputs(con: duckdb.DuckDBPyConnection) -> None:
    """Empty tables of the inputs' schema, to bind the statements against (check_binds)."""
    con.execute(
        """
        create or replace table orgunit (
            uid varchar, code varchar, name varchar, level integer, level_name varchar,
            parent_code varchar, parent_uid varchar, path varchar, type varchar,
            clerks integer, traits varchar, imported_to_dhis2 boolean,
            draws_records_and_users boolean
        )
        """
    )
    con.execute(
        "create or replace table clerk (username varchar, orgunit_uid varchar, n bigint, "
        "n_clerks bigint)"
    )


def split(script: str) -> list[str]:
    return [s.strip() for s in re.split(r";\n\n", script) if s.strip().rstrip(";")]


def check_binds(effective: dict, statements: list[str]) -> None:
    """Run the statements on empty inputs, so an expression DuckDB cannot bind fails compile."""
    con = duckdb.connect()
    sandbox.confine(con)
    prepare(con, effective)
    empty_inputs(con)
    for statement in statements:
        try:
            con.execute(statement)
        except duckdb.Error as e:
            # A statement may add the columns of many fields, each after a comment naming it:
            # the field is that of the last comment before the line DuckDB names, else the
            # statement's first.
            labels = re.findall(r"-- (program\.[^\n]+|trait\.[^\n]+)", statement)
            line = re.search(r"^LINE (\d+):", str(e), re.MULTILINE)
            if line:
                before = "\n".join(statement.splitlines()[: int(line.group(1))])
                labels = re.findall(r"-- (program\.[^\n]+|trait\.[^\n]+)", before)[-1:] or labels
            where = f" ({named(labels[0], keys(effective))})" if labels else ""
            raise UsageError(
                f"the spec's SQL does not bind{where}: {e}\n"
                "Check the expressions of the spec: the columns they name, such as "
                "facility.size, and their types."
            ) from e
    con.close()


def check_keys(con: duckdb.DuckDBPyConnection) -> None:
    """Fail on a key two records share: keys are 64-bit hashes, unique but by a chance of about
    n^2 / 2^65."""
    for table, key in (
        ("trackedentity", "te_key"),
        ("enrollment", "en_key"),
        ("event", "ev_key"),
        ("relationship", "rel_key"),
    ):
        [(dupes,)] = con.execute(
            f"select count(*) from (select {key} from out_{table} group by all having count(*) > 1)"
        ).fetchall()
        if dupes:
            raise InternalError(f"{dupes} {key} shared by two {table} records: a hash collision")


# The statements.

# A program or field in a label, by UID.
BY_UID = re.compile(r"\b(?:program|values|attributes)\.[A-Za-z][A-Za-z0-9]{10}\b")


def keys(effective: dict) -> dict[str, str]:
    """Each program and field as a label names it, by UID, and as errors do, by its key."""
    out = {}
    for p in effective["programs"]:
        out[f"program.{p['uid']}"] = f"program.{p['key']}"
        for v in [*p["attributes"], *(v for s in p["stages"] for v in s["values"])]:
            out[v["label"]] = f"{v['label'].partition('.')[0]}.{v['key']}"
    return out


def named(label: str, keys: dict[str, str]) -> str:
    """A label as errors name it: its program and field by their keys, not their UIDs."""
    return BY_UID.sub(lambda m: keys.get(m.group(0), m.group(0)), label)


class Program:
    """The SQL of one program's records."""

    def __init__(self, p: dict, effective: dict):
        self.p = p
        self.key = p["key"]
        self.k = table_suffix(self.key)
        self.effective = effective
        self.root = effective["orgunits"]["root"]
        self.levels: list[dict[str, Any]] = [
            {"name": self.root["name"], "slug": slug(self.root["name"]), "number": 1, "traits": {}},
            *effective["orgunits"]["levels"],
        ]
        self.level: dict[str, Any] = next(
            lv for lv in self.levels if lv["name"] == p["orgunit_level"]
        )
        self.place_slugs = [lv["slug"] for lv in self.levels[: self.level["number"]]]
        self.linked = p["from"]
        source = {self.linked["as"]} if self.linked else set()
        self.scopes = set(self.place_slugs) | set(SCOPES) | source
        # The scopes an expression sees at each place: those whose columns its table has.
        place = {*self.place_slugs, "orgunit"}
        enrollment = place | source | {"tracked_entity", "enrollment"}
        self.sees = {
            "place": place,  # tracked_entities: per_orgunit, total, by
            "source": place | source,  # from.where
            "tracked_entity": place | source | {"tracked_entity"},  # its traits, from.at, count
            "enrollment": enrollment,  # its traits, gap, a visit's or stage's count and offset
            "spacing": enrollment | {"event"},  # a visit's or stage's spacing: event.number
            "attribute": enrollment | {"record", "value"},
            "value": enrollment | {"event", "record", "value"},
        }
        # The scopes of the spec, a level below the program's too: an expression naming one it
        # does not see fails.
        self.known = {lv["slug"] for lv in self.levels} | set(SCOPES) | source
        self.source = (
            next(q for q in effective["programs"] if q["key"] == self.linked["enrollments"])
            if self.linked
            else None
        )
        self.keys = keys(effective)

    def label(self, *parts: str) -> str:
        # By UID, so that how the spec names a program changes none of its draws.
        return ".".join(["program", self.p["uid"], *parts])

    def where(self, label: str) -> str:
        """A label as errors name it: its program and field by their keys, not their UIDs."""
        return named(label, self.keys)

    def compile(self, spec, key: str, label: str, sees: str) -> str:
        """A distribution of the spec as SQL drawn for `key`, references rewritten, its
        expressions naming only the scopes of place `sees`."""
        where = self.where(label)

        def expression(sql: str) -> str:
            self.check(sql, sees, where)
            return expr.rewrite(sql, self.scopes, key, label, where)

        return distribution.Compiler(key, expression).compile(spec, label, where)

    def check(self, sql: str, sees: str, where: str) -> None:
        """Fail on a reference to a scope the expression does not see at place `sees`, or to a
        scope's name misspelt. Any other reference is SQL's, such as a lambda's p.lam."""
        visible = self.sees[sees]
        for scope, name in sorted(expr.references(sql)):
            if scope in visible:
                continue
            if scope in self.known:
                raise UsageError(
                    f"{where}: names {scope}.{name}, but sees only "
                    f"{', '.join(sorted(visible))} here"
                )
            close = difflib.get_close_matches(scope, self.known, n=1, cutoff=0.8)
            if close:
                raise UsageError(f"{where}: names {scope}.{name}: no scope {scope}; {close[0]}?")

    def days(self, spec, key: str, label: str, sees: str) -> str:
        """A duration or offset in whole days, floored."""
        return f"floor({self.compile(spec, key, label, sees)})::bigint"

    def traits(self, scope: str, key: str, table: str) -> list[str]:
        """Statements adding the scope's traits to table, in the spec's order."""
        out = []
        for name, spec in self.p["traits"][scope].items():
            label = f"trait.{scope}.{name}"
            sql = self.compile(spec, key, label, scope)
            out.append(
                f"-- {label}\ncreate or replace table {table} as select *, ({sql})::double "
                f'as "{scope}.{name}" from {table}'
            )
        return out

    def place(self) -> list[str]:
        """Table place_<k>: the org units of the program's level, with their scope columns."""
        joins, columns = [], []
        for lv in self.levels[: self.level["number"]]:
            s, n = lv["slug"], lv["number"]
            alias = f"l{n}"
            traits = ["weight", *lv.get("traits", {})]
            cols = [
                f'{alias}.code as "{s}.code"',
                f'{alias}.name as "{s}.name"',
                f'{alias}.uid as "{s}.uid"',
                f'{alias}.type as "{s}.type"',
            ] + [f"({alias}.traits::json->>'{t}')::double as \"{s}.{t}\"" for t in traits]
            columns += cols
            joins.append(
                f"join orgunit {alias} on {alias}.level = {n} "
                f"and {alias}.code = split_part(o.path, '/', {n})"
            )
        return [
            f"""-- program.{self.key}.orgunit_level
create or replace table place_{self.k} as
select o.uid as orgunit_uid, o.code as orgunit_code, o.draws_records_and_users,
       {", ".join(columns)},
       o.uid as "orgunit.uid", o.code as "orgunit.code"
from orgunit o
{chr(10).join(joins)}
where o.level = {self.level["number"]}"""
        ]

    def number(self, spec, key: str, label: str) -> str:
        """A number of the spec as SQL: a distribution, or a string, which is an expression."""
        return self.compile({"sql": spec} if isinstance(spec, str) else spec, key, label, "place")

    def mean_tracked_entities(self) -> str:
        """A query of the places with the mean of their tracked entities, `mean`: per_orgunit
        times `by`, or the total of the place's org unit of level `among` shared by `by`."""
        te = self.p["tracked_entities"]
        label = self.label("tracked_entities")
        by = (
            self.compile({"sql": te["by"]}, "p.orgunit_code", label + ".by", "place")
            if te["by"]
            else "1"
        )
        if te["total"] is None:
            per = self.number(te["per_orgunit"], "p.orgunit_code", label + ".per_orgunit")
            return f"select p.*, ({per}) * ({by}) as mean from place_{self.k} p"
        among = slug(te["among"])
        key = f'p."{among}.code"'
        total = self.number(te["total"], key, label + ".total")
        return f"""
            select * exclude (weight, total),
                   coalesce(total * weight
                            / nullif(sum(weight) over (partition by "{among}.code"), 0), 0) as mean
            from (select p.*, ({by}) as weight, {total} as total
                  from place_{self.k} p)"""

    def tracked_entities(self) -> list[str]:
        label = self.label("tracked_entities")
        # The first enrollment: a day of the window, uniform.
        first = (
            f"floor(getvariable('window_start') + rand_u(te_key, {quote(label + '.first')}) "
            "* (getvariable('window_end') - getvariable('window_start')))"
        )
        if self.linked:
            at = self.compile(
                {"sql": self.linked["at"]}, "te_key", self.label("from.at"), "tracked_entity"
            )
            first = f"case when source_en_key is null then {first} else floor({at}) end"
        statements = [
            f"""-- {label}
create or replace table te_{self.k} as
with counted as (
    -- The mean of every place, of the area or not, so a total is shared among all of its places;
    -- then the area's only.
    select * exclude (mean, draws_records_and_users),
           rand_poisson(orgunit_code, {quote(label)}, mean) as n
    from ({self.mean_tracked_entities()})
    where draws_records_and_users
),
numbered as (
    select * exclude (n), unnest(range(n)) as i from counted
)
select *, hash('{self.p["uid"]}', orgunit_code, i) as te_key, i as "tracked_entity.number"
from numbered""",
        ]
        if self.linked:
            statements.append(self.born())
        statements += self.traits("tracked_entity", "te_key", f"te_{self.k}")
        statements.append(
            f"""-- {label}.first
create or replace table te_{self.k} as
select *, {first} as first_day
from te_{self.k} p"""
        )
        return statements

    def source_columns(self) -> list[str]:
        """The columns of the source's scope: the source enrollment's traits, and its tracked
        entity's but where the enrollment has one of the name, its number, date, end and
        status."""
        assert self.source is not None
        columns = {
            name: f'm."{scope}.{name}"'
            for scope in ("tracked_entity", "enrollment")
            for name in self.source["traits"][scope]
        }
        columns |= {
            "number": 'm."enrollment.number"',
            "date": 'm."enrollment.date"',
            "end": "m.start_day + m.span",
            "status": "m.status",
        }
        return [f"{sql} as {expr.column(self.linked['as'], name)}" for name, sql in columns.items()]

    def born(self) -> str:
        """The statement adding to te_<k> the tracked entities linked from the source's
        enrollments: those of `where`, at `share`, each at its source's org unit."""
        f = self.linked
        assert f is not None and self.source is not None
        label = self.label("from")
        where = (
            self.compile({"sql": f["where"]}, "source_en_key", label + ".where", "source")
            if f["where"]
            else "true"
        )
        source = table_suffix(self.source["key"])
        return f"""-- {label}
create or replace table te_{self.k} as
select * from te_{self.k}
union all by name
select * from (
    select p.* exclude (draws_records_and_users), hash('{self.p["uid"]}', m.en_key) as te_key,
           0::bigint as "tracked_entity.number", m.te_key as source_te_key,
           m.en_key as source_en_key, {", ".join(self.source_columns())}
    from enr_{source} m
    join place_{self.k} p using (orgunit_uid)
    where p.draws_records_and_users
)
where rand_u(source_en_key, {quote(label + ".share")}) < {f["share"]!r} and ({where})"""

    def enrollments(self) -> list[str]:
        label = self.label("enrollments")
        count = self.compile(self.p["enrollments"], "te_key", label, "tracked_entity")
        gap = self.days(self.p["gap"], "en_key", self.label("gap"), "enrollment")
        statements = [
            f"""-- {label}
create or replace table en_{self.k} as
with counted as (
    select t.*, greatest(1, round({count}))::bigint as n_en from te_{self.k} t
),
numbered as (
    select * exclude (n_en), unnest(range(n_en)) as "enrollment.number" from counted
)
select *, hash(te_key, "enrollment.number") as en_key from numbered""",
            f"""-- {self.label("gap")}
create or replace table en_{self.k} as
select *, greatest({gap}, 0) as gap_days from en_{self.k}""",
        ]
        statements += self.traits("enrollment", "en_key", f"en_{self.k}")
        return statements

    def events(self) -> list[str]:
        """Table rel_<k>: the events of each enrollment, relative to its start: stage, visit
        (null outside the visits), number, offset in days and status. A count, a spacing and a
        status may name the enrollment's columns and traits, and a spacing the number of the
        event it leads to, event.number."""
        parts = []
        statements = []
        v = self.p["visits"]
        if v:
            label = self.label("visits")
            count = self.compile(v["count"], "en_key", label + ".count", "enrollment")
            spacing = self.days(v["spacing"], "hash(en_key, visit)", label + ".spacing", "spacing")
            statements.append(
                f"""-- {label}
create or replace table visit_{self.k} as
with counted as (
    select e.*, greatest(0, round({count}))::bigint as n_visits
    from en_{self.k} e
),
numbered as (
    select * exclude (n_visits), unnest(range(n_visits)) as visit from counted
),
spaced as (
    select *, case when visit = 0 then 0 else greatest({spacing}, 1) end as spacing
    from (select *, visit as "event.number" from numbered)
)
select en_key, visit,
       sum(spacing) over (partition by en_key order by visit
                          rows between unbounded preceding and current row) as offset_days,
       {self._status(v["status"], "hash(en_key, visit)", label + ".status")} as status
from spaced"""
            )
            for stage in v["stages"]:
                parts.append(
                    f"select en_key, '{stage}' as stage, visit, visit as number, offset_days, "
                    f"status from visit_{self.k}"
                )
            span = (
                f"(select en_key, max(offset_days) as visits_end from visit_{self.k} "
                "group by en_key)"
            )
        else:
            span = None
        for s in self.p["stages"]:
            if s["in_visits"]:
                continue
            label = self.label("stage", s["name"])
            key = f"hash(en_key, '{s['uid']}')"
            base = self.days(s["offset"], key, label + ".offset", "enrollment")
            if s["at"] == "end":
                if span is None:
                    raise UsageError(
                        f"{self.where(label)}.at: end needs [program.{self.key}.visits]"
                    )
                base = f"coalesce(v.visits_end, 0) + {base}"
            join = f"left join {span} v using (en_key)" if span and s["at"] == "end" else ""
            if s["repeatable"]:
                status = self._status(s["status"], f"hash({key}, number)", label + ".status")
                count = self.compile(s["count"], key, label + ".count", "enrollment")
                spacing = self.days(
                    s["spacing"], f"hash({key}, number)", label + ".spacing", "spacing"
                )
                statements.append(
                    f"""-- {label}
create or replace table stage_{self.k}_{s["uid"]} as
with counted as (
    select e.*, greatest(0, round({count}))::bigint as n_events, {base} as base
    from en_{self.k} e {join}
    where rand_u({key}, {quote(label + ".share")}) < {s["share"]!r}
),
numbered as (
    select * exclude (n_events), unnest(range(n_events)) as number from counted
),
spaced as (
    select *, case when number = 0 then 0 else greatest({spacing}, 1) end as spacing
    from (select *, number as "event.number" from numbered)
)
select en_key, '{s["uid"]}' as stage, null::bigint as visit, number,
       base + sum(spacing) over (partition by en_key order by number
                                 rows between unbounded preceding and current row) as offset_days,
       {status} as status
from spaced"""
                )
            else:
                statements.append(
                    f"""-- {label}
create or replace table stage_{self.k}_{s["uid"]} as
select e.en_key, '{s["uid"]}' as stage, null::bigint as visit, 0::bigint as number,
       greatest({base}, 0) as offset_days,
       {self._status(s["status"], key, label + ".status")} as status
from en_{self.k} e {join}
where rand_u({key}, {quote(label + ".share")}) < {s["share"]!r}"""
                )
            parts.append(f"select * from stage_{self.k}_{s['uid']}")
        if not parts:
            parts.append(
                "select null::ubigint as en_key, null::varchar as stage, null::bigint as visit, "
                "null::bigint as number, null::bigint as offset_days, null::varchar as status "
                "where false"
            )
        statements.append(
            f"create or replace table rel_{self.k} as\n" + "\nunion all\n".join(parts)
        )
        return statements

    def _status(self, shares: dict, key: str, label: str) -> str:
        c = distribution.Compiler(key)
        return c.categorical(list(shares.items()), label, self.where(label))

    def dates(self) -> list[str]:
        """Tables enr_<k> and ev_<k>: enrollments and events with their dates and statuses."""
        label = self.label("status")
        status = self._status(self.p["status"], "en_key", label)
        earlier = {s: w for s, w in self.p["status"].items() if s in ("COMPLETED", "CANCELLED")}
        earlier_status = (
            self._status(earlier, "en_key", label + ".earlier") if earlier else "'COMPLETED'"
        )
        return [
            f"""-- program.{self.key}: dates
create or replace table enr_{self.k} as
with spans as (
    select en_key, coalesce(max(offset_days), 0) as span from rel_{self.k} group by en_key
),
timed as (
    select e.*, coalesce(s.span, 0) as span,
           e.first_day + coalesce(sum(coalesce(s.span, 0) + e.gap_days) over (
               partition by e.te_key order by e."enrollment.number"
               rows between unbounded preceding and 1 preceding), 0) as start_day
    from en_{self.k} e
    left join spans s using (en_key)
),
kept as (
    select *, max("enrollment.number") over (partition by te_key) as last_number
    from timed
    where start_day < getvariable('window_end')
)
select *,
       start_day as "enrollment.date",
       case
           when start_day + span >= getvariable('window_end') then 'ACTIVE'
           when "enrollment.number" = last_number then {status}
           else {earlier_status}
       end as status
from kept""",
            f"""-- program.{self.key}: events
create or replace table ev_{self.k} as
with dated as (
    select r.*, e.start_day + r.offset_days as day, e.status as enrollment_status,
           e.te_key
    from rel_{self.k} r
    join enr_{self.k} e using (en_key)
),
due as (
    -- The first event of an enrollment still active from the window's end on: it is scheduled.
    select en_key, min(day) as due_day
    from dated
    where day >= getvariable('window_end') and enrollment_status = 'ACTIVE'
    group by en_key
)
select d.* exclude (status),
       case when d.day >= getvariable('window_end') then 'SCHEDULE' else d.status end as status,
       hash(d.en_key, d.stage, d.number) as ev_key,
       d.day as "event.date",
       d.number as "event.number"
from dated d
left join due using (en_key)
where d.day < getvariable('window_end') or d.day = due.due_day""",
        ]

    def values(self) -> list[str]:
        """Table val_<k>: the data values of each event with values, by stage."""
        statements, parts = [], []
        for s in self.p["stages"]:
            fields = [v for v in s["values"] if v["gen"] is not None]
            if not fields:
                continue
            table = f"val_{self.k}_{s['uid']}"
            statements.append(
                f"""-- {self.label("stage", s["name"])}: values
create or replace table {table} as
select ev.ev_key, ev."event.date", ev."event.number", ev.status as "event.status",
       ev."event.date" as "record.date", en.* exclude (status), en.status as "enrollment.status"
from ev_{self.k} ev
join enr_{self.k} en using (en_key)
where ev.status in ('COMPLETED', 'ACTIVE') and ev.stage = '{s["uid"]}'"""
            )
            statements += self._fields(table, fields, "ev_key", "value")
            items = ", ".join(f"{{'k': '{v['uid']}', 'v': {_column(v)}}}" for v in fields)
            parts.append(
                f"select ev_key, list_filter([{items}], lambda x: x.v is not null) as datavalues "
                f"from {table}"
            )
        if not parts:
            parts.append(
                "select null::ubigint as ev_key, "
                "[]::struct(k varchar, v varchar)[] as datavalues where false"
            )
        statements.append(
            f"create or replace table val_{self.k} as\n" + "\nunion all\n".join(parts)
        )
        return statements

    def _fields(self, table: str, fields: list[dict], key: str, sees: str) -> list[str]:
        """Statements adding a column "value.<UID>" of each field to table: the fields its
        expression names as value.<UID> first, the others in one statement per level. A field
        of a linked tracked entity takes its source's value, which attributes() joins as
        "linked.<UID>"."""
        statements = []
        for level in _levels(fields):
            columns = []
            for v in level:
                fill = (
                    "true"
                    if v["fill"] >= 1
                    else f"rand_u({key}, {quote(self.label(v['label'], 'fill'))}) < {v['fill']!r}"
                )
                if v.get("if"):
                    label = self.label(v["label"], "if")
                    self.check(v["if"], sees, self.where(label))
                    condition = expr.rewrite(v["if"], self.scopes, key, label, self.where(label))
                    fill += f" and ({condition})"
                sql = self.value_sql(v, key, sees) if v["gen"] is not None else "null"
                linked = (
                    f"when source_te_key is not null then {expr.column('linked', v['uid'])} "
                    if self.linked and v.get("linked")
                    else ""
                )
                columns.append(
                    f"-- {self.label(v['label'])}\n    case {linked}when {fill} "
                    f"then {sql} end as {_column(v)}"
                )
            statements.append(
                f"create or replace table {table} as\nselect *,\n    "
                + ",\n    ".join(columns)
                + f"\nfrom {table}"
            )
        return statements

    def value_sql(self, v: dict, key: str, sees: str) -> str:
        """The SQL of a field's value as DHIS2 stores it, a varchar, for the record of key, its
        expressions naming only the scopes of place `sees`."""
        label = self.label(v["label"])
        gen = v["gen"]
        vt = v["value_type"]
        if isinstance(gen, dict) and len(gen) == 1 and next(iter(gen)) in SPECIAL:
            name = next(iter(gen))
            return SPECIAL[name](self, gen[name], key, label, vt)
        if v["option_set"] or (isinstance(gen, dict) and "weights" in gen):
            return f"({self.compile(gen, key, label, sees)})::varchar"
        raw = self.compile(gen, key, label, sees)
        return format_value(raw, vt, gen)

    def attributes(self) -> list[str]:
        """Table att_<k>: the attribute values of each tracked entity, set at its registration,
        with the scope of its first enrollment."""
        table = f"atts_{self.k}"
        statements = [
            f"""-- program.{self.key}: attributes
create or replace table {table} as
select * exclude (status), status as "enrollment.status", "enrollment.date" as "record.date"
from enr_{self.k}
where "enrollment.number" = 0"""
        ]
        fields = [
            v
            for v in self.p["attributes"]
            if v["gen"] is not None or (self.linked and v.get("linked"))
        ]
        copied = [v for v in fields if self.linked and v.get("linked")]
        if copied:
            # The source's values of the attributes copied, as columns "linked.<UID>".
            source = table_suffix(self.linked["enrollments"])
            columns = ", ".join(
                f"a{i}.value as {expr.column('linked', v['uid'])}" for i, v in enumerate(copied)
            )
            joins = "\n".join(
                f"left join att_{source} a{i} on a{i}.te_key = t.source_te_key "
                f"and a{i}.attribute = '{v['linked']['from']}'"
                for i, v in enumerate(copied)
            )
            statements.append(
                f"""-- program.{self.key}.from.attributes
create or replace table {table} as
select t.*, {columns}
from {table} t
{joins}"""
            )
        statements += self._fields(table, fields, "te_key", "attribute")
        parts = [
            f"select te_key, '{v['uid']}' as attribute, {_column(v)} as value from {table}"
            for v in fields
        ]
        if not parts:
            parts.append(
                "select null::ubigint as te_key, null::varchar as attribute, "
                "null::varchar as value where false"
            )
        statements.append(
            f"create or replace table att_{self.k} as select * from (\n"
            + "\nunion all\n".join(parts)
            + "\n) where value is not null"
        )
        return statements

    def registrations(self) -> list[str]:
        """The statements up to the tracked entities."""
        return [*self.place(), *self.tracked_entities()]

    def records(self) -> list[str]:
        """The statements after the tracked entities' sequence numbers (sequence)."""
        return [
            f"""-- program.{self.key}: sequence numbers
create or replace table te_{self.k} as
select t.*, s.seq as "tracked_entity.seq"
from te_{self.k} t
join te_seq s using (te_key)""",
            *self.enrollments(),
            *self.events(),
            *self.dates(),
            *self.values(),
            *self.attributes(),
        ]


def _column(v: dict) -> str:
    """The column of a field's value, "value.<UID>", which an expression names as value.<UID>: a
    code may hold dots."""
    return expr.column("value", v["uid"])


def _strings(value) -> list[str]:
    """The strings of a generator, where its expressions are: JSON would quote them as strings,
    which expr.references leaves out."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _levels(fields: list[dict]) -> list[list[dict]]:
    """The fields in levels: those naming no other field's value.<UID> first, then those naming
    only fields of the levels before."""
    by_name = {f["uid"]: f for f in fields}
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def of(f: dict) -> int:
        if f["uid"] in depth:
            return depth[f["uid"]]
        if f["uid"] in visiting:
            raise UsageError(
                f"{f['label'].partition('.')[0]}.{f['key']}: its value names itself through "
                "value.<UID>"
            )
        visiting.add(f["uid"])
        names = [
            by_name[name]
            for text in [*_strings(f["gen"]), f.get("if") or ""]
            for scope, name in expr.references(text)
            if scope == "value" and name in by_name
        ]
        d = 1 + max((of(g) for g in names), default=-1)
        visiting.discard(f["uid"])
        depth[f["uid"]] = d
        return d

    for f in fields:
        of(f)
    levels: list[list[dict]] = [[] for _ in range(1 + max(depth.values(), default=-1))]
    for f in fields:
        levels[depth[f["uid"]]].append(f)
    return levels


def format_value(raw: str, value_type: str, gen) -> str:
    """The value of a distribution or an expression as DHIS2 stores a value of the value type.

    An expression may give text, such as 'true' or an option code, a number, or a date as a date
    or as days since 1970-01-01, which is what the spec's date distributions draw: the value is
    read through its text, so each works. What does not parse gives no value, which the pre-load
    checks of a compulsory field catch. A string of the spec is text, written as it is."""
    if isinstance(gen, str):
        return f"({raw})::varchar"
    v = f"(({raw})::varchar)"
    number = f"try_cast({v} as double)"
    if value_type in ("BOOLEAN", "TRUE_ONLY"):
        false = "'false'" if value_type == "BOOLEAN" else "null"
        return (
            f"case when lower({v}) in ('true', '1') or {number} > 0 then 'true' "
            f"when lower({v}) in ('false', '0') or {number} = 0 then {false} end"
        )
    if value_type.startswith("INTEGER"):
        return f"round({number})::bigint::varchar"
    if value_type in ("NUMBER", "UNIT_INTERVAL"):
        digits = gen.get("round", 1) if isinstance(gen, dict) else 1
        if digits == 0:
            return f"round({number})::bigint::varchar"
        return f"rtrim(rtrim(printf('%.{digits}f', {number}), '0'), '.')"
    if value_type == "PERCENTAGE":
        return f"least(greatest(round({number}), 0), 100)::bigint::varchar"
    if value_type in ("DATE", "AGE"):
        return (
            f"coalesce(strftime(try_cast({v} as date), '%Y-%m-%d'), "
            f"strftime(of_days({number}), '%Y-%m-%d'))"
        )
    if value_type == "DATETIME":
        seconds = f"to_seconds(floor(({number} - floor({number})) * 86400)::bigint)"
        return (
            f"coalesce(strftime(try_cast({v} as timestamp), '%Y-%m-%dT%H:%M:%S.000'), "
            f"strftime(of_days({number}) + {seconds}, '%Y-%m-%dT%H:%M:%S.000'))"
        )
    if value_type == "TIME":
        return f"strftime(time '00:00' + to_seconds(floor({number} * 86400)::bigint), '%H:%M')"
    return v


# Generators of value types a distribution does not draw, by name: each gives the SQL of a
# varchar value for the record of key.


WORDS_KEYS = ("n", "s", "q", "count", "vocabulary")


def _words(prog: Program, spec, key: str, label: str, vt: str) -> str:
    spec = spec if isinstance(spec, dict) else {}
    unknown = [k for k in spec if k not in WORDS_KEYS]
    if unknown:
        raise UsageError(
            f"{prog.where(label)}.words: unknown key {', '.join(unknown)}; words takes "
            f"{', '.join(WORDS_KEYS)}"
        )
    n, s, count = spec.get("n", 5000), spec.get("s", 1.1), spec.get("count", 1)
    q = spec.get("q", 0)
    vocabulary = spec.get("vocabulary", label)
    if type(n) is not int or type(count) is not int or n < 1 or count < 1:
        raise UsageError(f"{prog.where(label)}.words: n and count must be whole numbers, 1 or more")
    # A comparison with inf also fails nan, which TOML reads as a float.
    if isinstance(s, bool) or not isinstance(s, int | float) or not 0 < s < math.inf:
        raise UsageError(f"{prog.where(label)}.words: s must be a number above 0")
    if isinstance(q, bool) or not isinstance(q, int | float) or not 0 <= q < math.inf:
        raise UsageError(f"{prog.where(label)}.words: q must be a number, 0 or more")
    words = [
        f"word({quote(vocabulary)}, rand_zipf({key}, {quote(f'{label}#{i}')}, {n}, {s!r}, {q!r}))"
        for i in range(count)
    ]
    del prog, vt
    return " || ' ' || ".join(words) if count > 1 else words[0]


def _letter(prog: Program, spec, key: str, label: str, vt: str) -> str:
    del prog, spec, vt
    return f"chr(65 + (draw({key}, {quote(label)}) % 26)::integer)"


def _phone(prog: Program, spec, key: str, label: str, vt: str) -> str:
    """9 digits after the spare country code, the first 7, 8 or 9 as many mobile numbers start."""
    del prog, spec, vt
    return (
        f"'{PHONE_COUNTRY_CODE}' "
        f"|| (7 + (draw({key}, {quote(label + '#prefix')}) % 3))::varchar "
        f"|| lpad((draw({key}, {quote(label)}) % 100000000)::varchar, 8, '0')"
    )


def _email(prog: Program, spec, key: str, label: str, vt: str) -> str:
    del prog, spec, vt
    name = f"lower(word('email', draw({key}, {quote(label)}) % 100000))"
    number = f"(draw({key}, {quote(label + '#n')}) % 1000)::varchar"
    return f"{name} || {number} || '@example.org'"


def _url(prog: Program, spec, key: str, label: str, vt: str) -> str:
    del prog, spec, vt
    return f"'https://example.org/' || lower(word('url', draw({key}, {quote(label)}) % 100000))"


def _orgunit(prog: Program, spec, key: str, label: str, vt: str) -> str:
    del prog, spec, key, label, vt
    return '"orgunit.uid"'


def _username(prog: Program, spec, key: str, label: str, vt: str) -> str:
    """A clerk of the record's org unit: generate picks the record's own clerk later, so a
    username value names the clerk of its draw."""
    del prog, spec, vt
    return (
        f'(select c.username from clerk c where c.orgunit_uid = "orgunit.uid" '
        f"and c.n = draw({key}, {quote(label)}) % c.n_clerks)"
    )


def _unique(prog: Program, spec, key: str, label: str, vt: str) -> str:
    """A value from the tracked entity's sequence number, unique as it is: 12 digits, an affine
    permutation of it modulo 10^12, so neighbours look unrelated; a number for a numeric type."""
    del prog, spec, key, label
    seq = '"tracked_entity.seq"'
    if vt.startswith("INTEGER") or vt == "NUMBER":
        return f"({seq})::varchar"
    return f"lpad(((({seq})::hugeint * 7919 + 104729) % 1000000000000)::varchar, 12, '0')"


# A segment of a TextPattern that dbgen draws, with the + after it: a literal, or RANDOM or
# SEQUENTIAL of digits.
PATTERN_SEGMENT = re.compile(r'\s*(?:"([^"]*)"|(?:RANDOM|SEQUENTIAL)\((#+)\))\s*(?:\+|$)')


def _pattern(prog: Program, spec, key: str, label: str, vt: str) -> str:
    """A value of a TextPattern of literals and RANDOM or SEQUENTIAL digits, from the tracked
    entity's sequence number as _unique: unique while the sequence stays below 10^digits, which
    the pre-load checks check. Any other segment, such as RANDOM(X##) or ORG_UNIT_CODE(...),
    fails."""
    del key, vt
    pattern = spec if isinstance(spec, str) else spec.get("pattern", "")
    seq = '"tracked_entity.seq"'
    parts = []
    pos = 0
    while pos < len(pattern):
        m = PATTERN_SEGMENT.match(pattern, pos)
        if m is None:
            segment = pattern[pos:].split("+")[0].strip()
            raise UsageError(
                f"{prog.where(label)}: pattern {pattern!r}: dbgen draws only literals, "
                f"RANDOM(#..) and SEQUENTIAL(#..), not {segment}; give the field "
                "{ unique = true } instead"
            )
        literal, digits = m.groups()
        if literal is not None:
            parts.append(quote(literal))
        else:
            width = len(digits)
            modulus = 10**width
            # An affine permutation modulo 10^width: 48271 is prime, so coprime with it.
            parts.append(
                f"lpad(((({seq})::hugeint * 48271 + 12345678) % {modulus})::varchar, {width}, '0')"
            )
        pos = m.end()
    if not parts:
        raise UsageError(
            f"{prog.where(label)}: pattern {pattern!r}: dbgen draws only literals, RANDOM(#..) "
            "and SEQUENTIAL(#..)"
        )
    return " || ".join(parts)


SPECIAL = {
    "words": _words,
    "letter": _letter,
    "phone": _phone,
    "email": _email,
    "url": _url,
    "orgunit": _orgunit,
    "username": _username,
    "unique": _unique,
    "pattern": _pattern,
}


def lifecycle(effective: dict) -> list[str]:
    """The output tables: every program's records with their times, clerks and lifecycle."""
    programs = [Program(p, effective) for p in effective["programs"]]
    records = effective["records"]
    upd = records["updated"]
    after = distribution.Compiler("k").compile(upd["after"], "records.updated.after")
    entry_delay = distribution.Compiler("ev_key").compile(
        records["entry_delay"], "records.entry_delay"
    )
    # The share of each stage's events assigned to their clerk (the stage's assigned).
    shares = [
        f"when {quote(s['uid'])} then {s['assigned']!r}"
        for p in effective["programs"]
        for s in p["stages"]
        if s["assigned"] > 0
    ]
    assigned = f"case ev.stage {' '.join(shares)} else 0 end" if shares else "0"
    # The share of each program's enrollments followed up (its enrollments' followup).
    shares = [
        f"when {quote(p['uid'])} then {p['followup']!r}"
        for p in effective["programs"]
        if p["followup"] > 0
    ]
    followup = f"case e.program {' '.join(shares)} else 0 end" if shares else "0"
    ev_parts, en_parts, te_parts, att_parts, rel_parts = [], [], [], [], []
    for prog in programs:
        k = prog.k
        cocs = prog.p["category_option_combos"]
        if cocs:
            aoc = distribution.Compiler("ev_key").categorical(
                [(c, 1) for c in cocs], prog.label("aoc"), prog.label("aoc")
            )
        else:
            aoc = "null::varchar"
        ev_parts.append(
            f"""select ev.ev_key, ev.en_key, '{prog.p["uid"]}' as program, ev.stage,
       p.orgunit_uid as orgunit, ev.status, ev.day, ev.visit, ev.number,
       {aoc} as aoc,
       -- The day it was entered: its own, or for a scheduled event the previous one's.
       case when ev.status in ('SCHEDULE', 'SKIPPED')
            then e.start_day + coalesce(lag(ev.offset_days) over (
                partition by ev.en_key, ev.stage order by ev.number), 0)
            else ev.day + floor(greatest({entry_delay}, 0)) end as entry_day,
       v.datavalues
from ev_{k} ev
join enr_{k} e using (en_key)
join place_{k} p using (orgunit_uid)
left join val_{k} v using (ev_key)"""
        )
        en_parts.append(
            f"select en_key, te_key, '{prog.p['uid']}' as program, orgunit_uid as orgunit, "
            f'"enrollment.number" as number, start_day, status from enr_{k}'
        )
        te_parts.append(
            f"select te_key, '{prog.p['uid']}' as program, "
            f"'{prog.p['tracked_entity_type']}' as tracked_entity_type, orgunit_uid as orgunit "
            f"from te_{k} where te_key in (select te_key from enr_{k})"
        )
        att_parts.append(f"select * from att_{k}")
        f = prog.linked
        if f and f["relationship"]:
            ends = ["m.te_key", "c.te_key"]
            if not f["relationship_from_source"]:
                ends.reverse()
            # Linked a few minutes after the later of the two was registered, by its clerk.
            rel_parts.append(
                f"""select hash('{f["relationship_type"]}', t.te_key) as rel_key,
       '{f["relationship_type"]}' as relationship_type,
       {ends[0]} as from_te_key, {ends[1]} as to_te_key,
       greatest(m.created, c.created)
           + to_seconds(floor(60 + rand_u(t.te_key, 'relationship') * 540)::bigint) as created,
       case when m.created > c.created then m.clerk else c.clerk end as clerk,
       m.deleted or c.deleted as deleted
from te_{k} t
join timed_te c on c.te_key = t.te_key
join timed_te m on m.te_key = t.source_te_key"""
            )
    if not rel_parts:
        rel_parts.append(
            "select null::ubigint as rel_key, null::varchar as relationship_type, "
            "null::ubigint as from_te_key, null::ubigint as to_te_key, "
            "null::timestamp as created, null::varchar as clerk, null::boolean as deleted "
            "where false"
        )
    union = "\nunion all\n".join
    return [
        f"""-- records: events
create or replace table all_event as
{union(ev_parts)}""",
        """-- records: clerks and times of events
create or replace table timed_event as
select ev.*,
       of_days(ev.entry_day)::timestamp + interval 8 hour
           + to_seconds(floor(rand_u(ev.ev_key, 'created') * 9 * 3600)::bigint) as created,
       c.username as clerk
from all_event ev
join (select distinct orgunit_uid, n_clerks from clerk) nc on nc.orgunit_uid = ev.orgunit
join clerk c on c.orgunit_uid = ev.orgunit
  and c.n = draw(ev.ev_key, 'clerk') % nc.n_clerks""",
        f"""-- records: enrollments
create or replace table all_enrollment as
{union(en_parts)}""",
        """-- records: enrollments, created 5 minutes before their first event
create or replace table timed_enrollment as
with first_event as (
    select en_key, min(created) as first_created, arg_min(clerk, created) as clerk,
           max(created) filter (where status <> 'SCHEDULE') as last_created,
           arg_max(clerk, created) filter (where status <> 'SCHEDULE') as last_clerk,
           count(*) as events
    from timed_event group by en_key
)
select e.*,
       coalesce(f.first_created, of_days(e.start_day)::timestamp + interval 9 hour)
           - interval 5 minute as created,
       coalesce(f.clerk, c.username) as clerk,
       case when e.status = 'COMPLETED'
            then coalesce(f.last_created, of_days(e.start_day)::timestamp + interval 9 hour)
                 + interval 1 minute end as completed,
       case when e.status = 'COMPLETED' then coalesce(f.last_clerk, f.clerk, c.username)
            end as completed_by
from all_enrollment e
left join first_event f using (en_key)
join (select distinct orgunit_uid, n_clerks from clerk) nc on nc.orgunit_uid = e.orgunit
join clerk c on c.orgunit_uid = e.orgunit and c.n = draw(e.en_key, 'clerk') % nc.n_clerks""",
        f"""-- records: tracked entities, registered a minute before their first enrollment
create or replace table timed_te as
select t.*, f.created - interval 1 minute as created, f.clerk,
       rand_u(t.te_key, 'records.deleted') < {float(records["deleted"])!r} as deleted
from ({union(te_parts)}) t
join (select te_key, min(created) as created, arg_min(clerk, created) as clerk
      from timed_enrollment group by te_key) f using (te_key)""",
        f"""-- records.updated: a share of records updated after their creation
create or replace table updates as
select kind, k, least(created + to_seconds(floor(86400 * greatest({after}, 0))::bigint),
                      of_days(getvariable('now'))::timestamp) as updated
from (
    select 'event' as kind, ev_key as k, created from timed_event
    union all select 'enrollment', en_key, created from timed_enrollment
    union all select 'trackedentity', te_key, created from timed_te
)
where rand_u(k, 'records.updated') < {float(upd["share"])!r}""",
        f"""-- records: events
create or replace table out_event as
select ev.ev_key, ev.en_key, ev.program, ev.stage, ev.orgunit,
       ev.status,
       case when ev.status in ('SCHEDULE', 'SKIPPED') then null else of_days(ev.day) end
           as occurred,
       of_days(ev.day) as scheduled,
       ev.created,
       greatest(coalesce(u.updated, ev.created),
                ev.created + to_seconds(floor(rand_u(ev.ev_key, 'lastupdated') * 1800)::bigint))
           as lastupdated,
       case when ev.status = 'COMPLETED' then ev.created end as completed,
       ev.clerk,
       case when rand_u(ev.ev_key, 'stage.assigned') < {assigned} then ev.clerk end
           as assigned,
       ev.aoc, coalesce(t.deleted, false) as deleted,
       coalesce(ev.datavalues, []) as datavalues
from timed_event ev
join all_enrollment e using (en_key)
join timed_te t using (te_key)
left join updates u on u.kind = 'event' and u.k = ev.ev_key""",
        f"""-- records: enrollments
create or replace table out_enrollment as
select e.en_key, e.te_key, e.program, e.orgunit, e.number, of_days(e.start_day) as enrollment_date,
       e.status, e.created,
       greatest(e.created, coalesce(u.updated, e.created), coalesce(e.completed, e.created),
                coalesce((select max(lastupdated) from out_event o where o.en_key = e.en_key),
                         e.created)) as lastupdated,
       e.completed, e.clerk, e.completed_by,
       rand_u(e.en_key, 'enrollments.followup') < {followup} as followup,
       t.deleted
from timed_enrollment e
join timed_te t using (te_key)
left join updates u on u.kind = 'enrollment' and u.k = e.en_key""",
        """-- records: tracked entities
create or replace table out_trackedentity as
select t.te_key, t.program, t.tracked_entity_type, t.orgunit, t.created,
       greatest(coalesce(u.updated, t.created),
                (select max(lastupdated) from out_enrollment e where e.te_key = t.te_key))
           as lastupdated,
       t.clerk, t.deleted
from timed_te t
left join updates u on u.kind = 'trackedentity' and u.k = t.te_key""",
        f"""-- records: attribute values
create or replace table out_attributevalue as
select a.te_key, a.attribute, a.value, t.created, t.created as lastupdated, t.clerk
from ({union(att_parts)}) a
join timed_te t using (te_key)""",
        f"""-- records: relationships
create or replace table out_relationship as
{union(rel_parts)}""",
    ]


def statements(effective: dict) -> list[str]:
    """The statements of generate, which compile writes to generate.sql: first the programs
    linked from none, then those linked from them, whose tracked entities their enrollments
    give."""
    programs = [Program(p, effective) for p in effective["programs"]]
    out = []
    for i, batch in enumerate(
        ([p for p in programs if not p.linked], [p for p in programs if p.linked])
    ):
        if not batch:
            continue
        for prog in batch:
            out += prog.registrations()
        # Every tracked entity's sequence number, from 1 across all programs, which unique values
        # derive from: a batch's numbers follow the batch's before.
        union = "\nunion all\n".join(
            f"select '{prog.p['uid']}' as program, te_key from te_{prog.k}" for prog in batch
        )
        before = "(select coalesce(max(seq), 0) from te_seq)" if i else "0"
        out.append(
            f"""-- tracked entities: sequence numbers
create or replace table te_seq as
{"select * from te_seq union all" if i else ""}
select te_key, {before} + row_number() over (order by program, te_key) as seq
from ({union})"""
        )
        for prog in batch:
            out += prog.records()
    out += lifecycle(effective)
    return out
