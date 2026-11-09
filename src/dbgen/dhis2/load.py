"""`dbgen stage load`: write generate's tracker tables straight into the build database.

This stage exists because the tracker data is too large for DHIS2's tracker API. It writes the rows
with Postgres COPY instead, and the checks make up for what the importer would validate (see
"Checks" in the README).

1. Check the columns against the schema DHIS2 created, so that a layout that does not fit the
   pinned DHIS2 image fails before anything changes.
2. Require empty tables: fail on one that holds rows, or with --replace truncate them all. Only
   then check Docker has the disk for the load, as truncating gives back the rows' space.
3. Drop the secondary indexes and foreign keys: building them once after the load is faster than
   updating and checking them for every row (Postgres docs, "Populating a Database").
4. Insert each table through DuckDB, which reads the Parquet files, builds eventdatavalues on the
   way (sql/event_json.sql) and writes through its postgres extension, which turns an insert into
   a COPY: binary, or text into a table with a jsonb column. How depends on the spec's load.order:

   * created (the default): the rows of a table in one stream, in the order of their ids, which
     follow their creation, as a live instance inserts them. Then the rows updated after their
     creation (lastupdated after created) are updated in the order of their update, which writes a
     new version of each row where the table has room, at its end, as a live instance's updates do:
     the order on disk is then not the order of creation, so the planner's correlation of the
     dates is not near 1, as on a live instance.
   * parallel: each table split by its SPLIT_KEY into a range per CPU of Postgres (streams()),
     loaded at once over a connection each: faster, as a connection is served by one Postgres
     backend, a single process, which parsing jsonb keeps at 100% of one core. The ranges
     interleave on disk.
5. VACUUM the tables, as autovacuum is off (docker/compose.yaml), and before the indexes: the
   first scan of a new row sets its hint bits, and vacuum marks its page all-visible, each
   writing the page. Vacuum does both in one pass, whereas the first index build would write
   every page and the vacuum after it every page again.
6. Add back the indexes and foreign keys, which validates every row. Set the sequences past the
   loaded ids, so DHIS2 numbers its new rows after them, and ANALYZE, so the planner has
   statistics of the rows loaded. Analyze comes last, as it takes statistics of an index
   expression, such as lower(value) of the attribute values, only while the index exists.

DHIS2 must not run during a load: its ownership and Hibernate caches would go stale.
"""

import json
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import LiteralString, cast

import duckdb
import psycopg
import pyarrow.parquet as pq

from dbgen import compile as compile_stage
from dbgen import provenance, sqlfile, timing
from dbgen.config import Build, listed
from dbgen.dhis2 import client
from dbgen.dhis2.bootstrap import IDMAP_QUERIES
from dbgen.errors import InternalError, UsageError
from dbgen.generate import TABLES

# The id column of each table, and the sequence DHIS2 numbers its new rows from: one of the
# table's own, or hibernate_sequence, which numbers the metadata and the users too (the Hibernate
# mappings of 2.42).
IDS = {
    "trackedentity": ("trackedentityid", "trackedentityinstance_sequence"),
    "enrollment": ("enrollmentid", "programinstance_sequence"),
    "event": ("eventid", "programstageinstance_sequence"),
    "trackedentityprogramowner": ("trackedentityprogramownerid", "hibernate_sequence"),
    "relationship": ("relationshipid", "hibernate_sequence"),
    "relationshipitem": ("relationshipitemid", "hibernate_sequence"),
}

# Column that splits a table into ranges for the streams: its id, and for the attribute values,
# which have none, their tracked entity's. generate orders each Parquet file by it, so a stream
# reads only the row groups of its range.
SPLIT_KEY = {table: column for table, (column, _) in IDS.items()} | {
    "trackedentityattributevalue": "trackedentityid"
}

# The columns of generate's tables that reference metadata by UID, and what load writes in their
# place: (the database's column, the ID map, its column). An ID map is bootstrap's (IDMAP_QUERIES),
# or user_snapshot, a user's snapshot as DHIS2 keeps it in createdbyuserinfo, by user UID.
RESOLVE = {
    "organisationunit_uid": ("organisationunitid", "organisationunit", "organisationunitid"),
    "trackedentitytype_uid": ("trackedentitytypeid", "trackedentitytype", "trackedentitytypeid"),
    "program_uid": ("programid", "program", "programid"),
    "programstage_uid": ("programstageid", "programstage", "programstageid"),
    "attributeoptioncombo_uid": (
        "attributeoptioncomboid",
        "categoryoptioncombo",
        "categoryoptioncomboid",
    ),
    "trackedentityattribute_uid": (
        "trackedentityattributeid",
        "trackedentityattribute",
        "trackedentityattributeid",
    ),
    "relationshiptype_uid": ("relationshiptypeid", "relationshiptype", "relationshiptypeid"),
    "assigneduser_uid": ("assigneduserid", "userinfo", "userinfoid"),
    "createdby_uid": ("createdbyuserinfo", "user_snapshot", "snapshot"),
    "lastupdatedby_uid": ("lastupdatedbyuserinfo", "user_snapshot", "snapshot"),
}
# A relationship keeps the user who last updated it by internal ID, not as a snapshot.
RESOLVE_OF = {"relationship": {"lastupdatedby_uid": ("lastupdatedby", "userinfo", "userinfoid")}}
# Columns computed while loading from the resolved columns, by the expression that computes each,
# and the columns of generate's tables only read to compute them.
DERIVED = {
    "event": {
        "eventdatavalues": (
            "event_json(datavalues, created, lastupdated, createdbyuserinfo, lastupdatedbyuserinfo)"
        ),
    },
}
DERIVED_INPUTS = {"event": {"datavalues"}}


def run(build: Build, replace: bool = False) -> None:
    effective, parent_stages = compile_stage.inputs(build, "load")
    order = effective["load"]["order"]
    output = build.make_stage_dir("load")
    steps: dict[str, float] = {}
    with timing.timed(build.dir, "load") as t:
        # A DHIS2 started by hand would cache what the load replaces, and `up db` leaves it running.
        client.stop(build.project, ("web", "web-healthcheck"))
        with client.running(build.project, ("db",), timeout=300):
            with client.connect(build.project, autocommit=True) as pg, pg.cursor() as cur:
                bootstrap_run = parent_stages["bootstrap"]["run"]
                provenance.check_database(cur, bootstrap_run)
                columns = check_columns(cur, build)
                ensure_empty(cur, replace)
                # The tables no longer hold what the last load recorded, so a load that fails from
                # here on must leave no record, which verify and dump would take for its own.
                provenance.forget(build, "load")
                # Only now, as with --replace the rows truncated give back their space.
                check_disk(build)
                constraints = drop_constraints(cur, output / "constraints.json", bootstrap_run)
                insert_tables(build, columns, steps, streams=1 if order == "created" else streams())
                if order == "created":
                    update_rows(cur, build, steps)
                vacuum(cur, steps)
                add_constraints(cur, constraints, steps)
                set_sequences(cur)
                analyze(cur, steps)
                t.add(**{f"rows_{table}": _rows(cur, table) for table in TABLES})
    (output / "steps.json").write_text(json.dumps(steps, indent=2, sort_keys=True) + "\n")
    slowest = dict(sorted(steps.items(), key=lambda kv: -kv[1])[:8])
    print(f"load: slowest steps {listed(slowest, 's')}", file=sys.stderr)
    provenance.record(build, "load", parent_stages, effective)


def streams() -> int:
    """Parallel insert streams per table: the CPUs compose.yaml gives Postgres."""
    return max(1, int(float(client.caps()["DBGEN_DB_CPUS"])))


def check_columns(cur: psycopg.Cursor, build: Build) -> dict[str, list[str]]:
    """The columns to load of each table. Fails on a column the table does not have, and on a
    NOT NULL column without a default the rows lack: a layout that does not fit the pinned DHIS2
    image."""
    problems, columns = [], {}
    for table in TABLES:
        live = _columns(cur, table)
        if not live:
            problems.append(f"{table}: table missing from the schema")
            continue
        rows = [
            _resolved(table, c)[0]
            for c in pq.read_schema(build.stage_dir("generate") / f"{table}.parquet").names
            if c not in DERIVED_INPUTS.get(table, set())
        ] + list(DERIVED.get(table, {}))
        unknown = [c for c in rows if c not in live]
        required = [c for c, (nullable, default) in live.items() if not nullable and not default]
        missing = [c for c in required if c not in rows]
        if unknown:
            problems.append(f"{table}: columns not in the schema: {unknown}")
        if missing:
            problems.append(f"{table}: NOT NULL columns without default not generated: {missing}")
        columns[table] = rows
    if problems:
        raise InternalError(
            "the generated tables do not fit the schema DHIS2 created:\n  " + "\n  ".join(problems)
        )
    return columns


def _columns(cur: psycopg.Cursor, table: str) -> dict[str, tuple[bool, bool]]:
    """{column: (nullable, has default)} of a table in the live schema."""
    cur.execute(
        """
        select column_name, is_nullable = 'YES',
               column_default is not null or is_generated = 'ALWAYS'
        from information_schema.columns
        where table_schema = 'public' and table_name = %s
        order by ordinal_position
        """,
        [table],
    )
    return {name: (nullable, default) for name, nullable, default in cur.fetchall()}


def ensure_empty(cur: psycopg.Cursor, replace: bool) -> None:
    """Truncate the tables with `replace`, and fail on one that holds rows without it."""
    if replace:
        # Only ever the build database: the tables referencing these (notes, relationships,
        # messages) hold nothing but generated data there too.
        _execute(cur, f"truncate {', '.join(TABLES)} cascade")
        return
    for table in TABLES:
        # Stops at the first row, where count(*) would scan every one of a loaded table.
        _execute(cur, f"select exists (select from {table})")
        [(nonempty,)] = cur.fetchall()
        if nonempty:
            raise UsageError(
                f"table {table} already holds rows; dbgen stage load --replace empties the "
                "tracker tables to load them again"
            )


# Database growth per million events, from the size of a region's build database, plus the WAL
# docker/compose.yaml allows (max_wal_size) and headroom.
GIB_PER_MILLION_EVENTS = 1.5
WAL_GIB = 4
DISK_HEADROOM_GIB = 4


def check_disk(build: Build) -> None:
    """Fail unless Docker's filesystem has room for the events of the build."""
    events = pq.read_metadata(build.stage_dir("generate") / "event.parquet").num_rows
    needed = GIB_PER_MILLION_EVENTS * events / 1e6 + WAL_GIB + DISK_HEADROOM_GIB
    free = client.docker_disk_free_gib()
    if free < needed:
        raise UsageError(
            f"loading {events:,} events needs about {needed:.0f} GiB where Docker keeps its data, "
            f"only {free:.1f} GiB free. Free some (docker system df shows what uses it)."
        )


def drop_constraints(
    cur: psycopg.Cursor, path: Path, bootstrap_run: str
) -> list[tuple[str, str, str]]:
    """Drop the secondary indexes and foreign keys of the tables, which every insert would update
    or check, and return them (see _constraints).

    The first load of a bootstrap run's database saves them to `path`, and the loads after it take
    them from there: a load that fails before it has added them all back leaves the rest missing,
    and the next would neither find them to drop nor add them back.
    """
    saved = json.loads(path.read_text()) if path.exists() else {}
    if saved.get("bootstrap_run") == bootstrap_run:
        constraints = [tuple(c) for c in saved["constraints"]]
    else:
        constraints = _constraints(cur)
        path.write_text(
            json.dumps({"bootstrap_run": bootstrap_run, "constraints": constraints}, indent=2)
            + "\n"
        )
    # The foreign keys first, as one may depend on an index.
    for _, _, drop in reversed(constraints):
        _execute(cur, drop)
    print(f"load: dropped {len(constraints)} indexes and foreign keys", file=sys.stderr)
    return constraints


def _constraints(cur: psycopg.Cursor) -> list[tuple[str, str, str]]:
    """The secondary indexes of the tables, those not behind a constraint such as a primary key,
    then their foreign keys, each as its step in steps.json and the statements that add it and drop
    it if it exists. A foreign key's step names its table too: relationship and trackedentity both
    have fk_lastupdateby_userid."""
    cur.execute(
        """
        select i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid)
        from pg_index i
        where i.indrelid::regclass::text = any(%s)
          and not exists (select 1 from pg_constraint c where c.conindid = i.indexrelid)
        order by 1
        """,
        [TABLES],
    )
    indexes = [
        (f"index {name}", add, f"drop index if exists {name}") for name, add in cur.fetchall()
    ]
    cur.execute(
        """
        select conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        from pg_constraint
        where contype = 'f' and conrelid::regclass::text = any(%s)
        order by 1, 2
        """,
        [TABLES],
    )
    fks = [
        (
            f"fk {table}.{name}",
            f'alter table {table} add constraint "{name}" {definition}',
            f'alter table {table} drop constraint if exists "{name}"',
        )
        for table, name, definition in cur.fetchall()
    ]
    return indexes + fks


def _duckdb(build: Build) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection that spills beside the stage's outputs, not into .tmp of the working
    directory, which may lie on another, smaller disk."""
    con = duckdb.connect()
    con.execute("set temp_directory = ?", [str(build.make_stage_dir("load") / "duckdb.tmp")])
    return con


def insert_tables(
    build: Build, columns: dict[str, list[str]], steps: dict[str, float], streams: int
) -> None:
    """Insert every table through DuckDB, timing each as a step. columns: by table, the columns to
    load (check_columns)."""
    with _duckdb(build) as con:
        con.execute("set memory_limit = '4GB'; set threads = 4")
        con.execute("install postgres; load postgres")
        con.execute(f"attach '{client.dsn(build.project)}' as pg (type postgres)")
        for statement in sqlfile.script(sqlfile.SQL / "event_json.sql"):
            con.execute(statement)
        id_maps(con, build)
        for table in TABLES:
            con.execute(f"create or replace view source_{table} as {_source(con, build, table)}")
        check_resolved(con)
        for table in TABLES:
            with _step(steps, f"copy {table}"):
                _insert(con, build, table, columns[table], streams)


def _resolved(table: str, column: str) -> tuple[str, str | None, str | None]:
    """The database's column of a column of generate's table, and the ID map and its column that
    resolve it, or None when it is loaded as it is."""
    spec = RESOLVE_OF.get(table, {}).get(column) or RESOLVE.get(column)
    return spec if spec else (column, None, None)


def id_maps(con: duckdb.DuckDBPyConnection, build: Build) -> None:
    """Tables b_<name> of bootstrap's ID maps, and b_user_snapshot, each user's snapshot as DHIS2
    writes it to createdbyuserinfo: its internal ID, UID, username and names."""
    for name in IDMAP_QUERIES:
        con.execute(
            f"create or replace table b_{name} as select * from read_parquet(?)",
            [str(build.stage_dir("bootstrap") / f"{name}.parquet")],
        )
    con.execute(
        """
        create or replace table b_user_snapshot as
        select uid, to_json(struct_pack(id := userinfoid, uid := uid, username := username,
                                        firstName := firstname, surname := surname))::varchar
                   as snapshot
        from b_userinfo
        """
    )
    found = con.execute(
        "select categoryoptioncomboid from b_categoryoptioncombo where name = 'default'"
    ).fetchall()
    if len(found) != 1:
        raise InternalError("bootstrap's ID maps hold no default category option combo")
    # A literal in the views of _source rather than a variable, which the cursors of the streams,
    # connections of their own, would not see.
    con.execute("create or replace table b_default_coc as select ? as id", [found[0][0]])


def _source(con: duckdb.DuckDBPyConnection, build: Build, table: str) -> str:
    """A query of generate's table with each UID resolved to the database's column (RESOLVE)."""
    path = build.stage_dir("generate") / f"{table}.parquet"
    [(default_coc,)] = con.execute("select id from b_default_coc").fetchall()
    columns, joins = [], []
    for i, column in enumerate(pq.read_schema(path).names):
        target, map_name, map_column = _resolved(table, column)
        if map_name is None:
            columns.append(f"t.{column}")
            continue
        alias = f"m{i}"
        joins.append(f"left join b_{map_name} {alias} on {alias}.uid = t.{column}")
        value = f"{alias}.{map_column}"
        if column == "attributeoptioncombo_uid":
            value = f"coalesce({value}, {int(default_coc)})"
        columns.append(f"{value} as {target}")
        # The UID itself, to find one the maps lack (check_resolved).
        columns.append(f"t.{column} as {column}")
    return f"select {', '.join(columns)} from read_parquet('{path}') t " + " ".join(joins)


def check_resolved(con: duckdb.DuckDBPyConnection) -> None:
    """Fail on a UID of generate's tables that bootstrap's ID maps lack: the metadata or users it
    imported are not those generate read."""
    problems = []
    for table in TABLES:
        columns = [d[0] for d in con.execute(f"select * from source_{table} limit 0").description]
        for column in columns:
            target = _resolved(table, column)[0]
            if not column.endswith("_uid") or target == column:
                continue
            if column == "attributeoptioncombo_uid":
                where = (
                    f"{column} is not null "
                    f"and {column} not in (select uid from b_categoryoptioncombo)"
                )
            else:
                where = f"{column} is not null and {target} is null"
            [(n, example)] = con.execute(
                f"select count(*), any_value({column}) from source_{table} where {where}"
            ).fetchall()
            if n:
                problems.append(f"{table}.{column}: {n} rows, e.g. {example}")
    if problems:
        raise UsageError(
            "generate's tables reference UIDs bootstrap did not import:\n  "
            + "\n  ".join(problems)
            + "\nrerun bootstrap and generate from one run of compile, orgunits and users"
        )


@contextmanager
def _step(steps: dict[str, float], name: str) -> Generator[None]:
    """Time the enclosed block as step `name` of steps.json."""
    start = time.perf_counter()
    yield
    steps[name] = round(time.perf_counter() - start, 2)


def _insert(
    con: duckdb.DuckDBPyConnection, build: Build, table: str, columns: list[str], streams: int
) -> None:
    """Insert `columns` of one table in `streams` parallel streams, each a key range of the
    Parquet file. One stream inserts the rows in the order of the key."""
    key = SPLIT_KEY[table]
    [(low, high)] = con.execute(f"select min({key}), max({key}) from source_{table}").fetchall()
    if low is None:
        return  # nothing to load, e.g. no relationships in the area the build covers
    step = (high - low) // streams + 1
    select = ", ".join(DERIVED.get(table, {}).get(c, c) for c in columns)
    # One stream inserts in the order of the key, as generate wrote the file.
    order = f" order by {key}" if streams == 1 else ""

    def stream(i: int) -> None:
        with con.cursor() as cursor:
            cursor.execute(
                f"insert into pg.public.{table} ({', '.join(columns)}) "
                f"select {select} from source_{table} "
                f"where {key} >= {low + i * step} and {key} < {low + (i + 1) * step}{order}"
            )

    # The pool waits for every stream before it raises the error of one.
    with ThreadPoolExecutor(max_workers=streams) as pool:
        list(pool.map(stream, range(streams)))


# Rows updated per statement of update_rows: a batch is written in the order of its scan, the
# batches in the order of their updates.
UPDATE_BATCH = 10_000


def update_rows(cur: psycopg.Cursor, build: Build, steps: dict[str, float]) -> None:
    """Update the rows generate lists in updated.parquet, in the order of their update, setting
    their lastupdated: each update writes a new version of its row, as a live instance's do."""
    path = build.stage_dir("generate") / "updated.parquet"
    if not path.exists():
        return
    with _step(steps, "update"), _duckdb(build) as con:
        rows = con.execute(
            "select tbl, id, lastupdated from read_parquet(?) order by lastupdated, tbl, id",
            [str(path)],
        ).fetchall()
        for start in range(0, len(rows), UPDATE_BATCH):
            batch = rows[start : start + UPDATE_BATCH]
            by_table: dict[str, list[tuple]] = {}
            for table, id_, lastupdated in batch:
                by_table.setdefault(table, []).append((id_, lastupdated))
            for table, pairs in by_table.items():
                key = SPLIT_KEY[table]
                _execute(
                    cur, "create temp table if not exists upd (id bigint, lastupdated timestamp)"
                )
                _execute(cur, "truncate upd")
                with cur.copy("copy upd (id, lastupdated) from stdin") as copy:
                    for pair in pairs:
                        copy.write_row(pair)
                _execute(
                    cur,
                    f"update {table} t set lastupdated = u.lastupdated from upd u "
                    f"where t.{key} = u.id",
                )
    print(f"load: updated {len(rows)} rows after their creation", file=sys.stderr)


def vacuum(cur: psycopg.Cursor, steps: dict[str, float]) -> None:
    """Vacuum the tables, before add_constraints so each page is written once (see the module
    docstring)."""
    with _step(steps, "vacuum"):
        for table in TABLES:
            _execute(cur, f"vacuum {table}")


def add_constraints(
    cur: psycopg.Cursor, constraints: list[tuple[str, str, str]], steps: dict[str, float]
) -> None:
    """Add back what drop_constraints dropped, each a step: the indexes, then the foreign keys."""
    for step, add, _ in constraints:
        with _step(steps, step):
            _execute(cur, add)


def set_sequences(cur: psycopg.Cursor) -> None:
    """Set each sequence past the largest id of the tables DHIS2 numbers from it, so it numbers
    its new rows after those loaded. A sequence only moves forward, as hibernate_sequence numbers
    the metadata and the users too."""
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, (column, sequence) in IDS.items():
        tables.setdefault(sequence, []).append((table, column))
    for sequence, of in tables.items():
        # greatest skips the null max of an empty table.
        ids = ", ".join(f"(select max({column}) from {table})" for table, column in of)
        _execute(
            cur,
            f"select setval('{sequence}', greatest((select last_value from {sequence}), {ids}))",
        )


def analyze(cur: psycopg.Cursor, steps: dict[str, float]) -> None:
    """Analyze the database, after add_constraints so the statistics cover the index expressions
    (see the module docstring). All of it, not only the tracker tables: autovacuum is off in the
    build, so the metadata tables DHIS2 wrote, and the empty ones such as note, would otherwise
    have no statistics, and the planner would guess their rows."""
    with _step(steps, "analyze"):
        _execute(cur, "analyze")


def _rows(cur: psycopg.Cursor, table: str) -> int:
    _execute(cur, f"select count(*) from {table}")
    [(rows,)] = cur.fetchall()
    return rows


def _execute(cur: psycopg.Cursor, statement: str) -> None:
    """Run a statement built from our own table names or from DDL the catalog returned.

    psycopg types execute() to take only literal strings, which keeps outside input out of SQL.
    These statements hold none, so they are passed on as they are.
    """
    cur.execute(cast(LiteralString, statement))
