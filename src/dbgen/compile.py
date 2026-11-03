"""`dbgen stage compile`: the effective spec, checked, and the SQL that generates its records.

Reads the spec and the metadata files it names, gives every field its generator (spec.py), and
fails on what would fail later: a name the metadata does not hold, an option code an option set
does not hold, a compulsory field without a value, an expression DuckDB cannot bind (it binds the
SQL of generate against empty tables, so a typo in an expression fails here, in a second).

Writes:

* effective.json: the effective spec, every field with its generator and where that came from
  (default or spec), which `dbgen explain` prints
* generate.sql: the statements generate runs
* metadata/<table>.parquet: what the pre-load checks of generate read of the metadata (metadata.py)
"""

import hashlib
import json
import sys
from pathlib import Path

import duckdb

from dbgen import provenance, spec
from dbgen.config import Build, shown
from dbgen.errors import UsageError
from dbgen.metadata import SCHEMAS, Metadata, read
from dbgen.metadata import tables as metadata_tables


def run(build: Build) -> dict:
    effective, metadata = compile_build(build)
    parents = provenance.check(build, "compile", effective)
    provenance.forget(build, "compile")
    output = build.make_stage_dir("compile")
    from dbgen import generate  # here, as generate imports compile

    statements = generate.statements(effective)
    generate.check_binds(effective, statements)
    (output / "generate.sql").write_text(";\n\n".join(statements) + ";\n")
    write_metadata(effective, metadata, output / "metadata")
    (output / "effective.json").write_text(json.dumps(effective, indent=1, default=str) + "\n")
    n_fields = sum(len(s["values"]) for p in effective["programs"] for s in p["stages"])
    n_programs = len(effective["programs"])
    print(
        f"compile: {shown(build.spec)}, {n_programs} program{'s' if n_programs != 1 else ''} "
        f"with {n_fields:,} data elements in their stages",
        file=sys.stderr,
    )
    # The run of compile is its output: compiling the same spec again keeps the stages that read
    # it valid, as their inputs did not change.
    run_id = hashlib.sha256(
        json.dumps(effective, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    provenance.record(build, "compile", parents, effective, run=run_id)
    return effective


def compile_build(build: Build) -> tuple[dict, Metadata]:
    """The effective spec of the build, with every field's generator, and its metadata."""
    effective, metadata = spec.compile_spec(build.spec, build.codes)
    for entry, path in zip(effective["build"]["metadata"], metadata.files, strict=True):
        entry["sha256"] = metadata.sha256[str(path)]
    problems = spec.apply_defaults(effective, metadata)
    if problems:
        raise UsageError(
            f"spec {shown(build.spec)} does not fit its metadata:\n  " + "\n  ".join(problems)
        )
    return effective, metadata


def current_metadata(effective: dict) -> Metadata:
    """The metadata of the effective spec, failing if a file changed since compile read it."""
    files = [Path(m["path"]) for m in effective["build"]["metadata"]]
    metadata = read(files)
    changed = [
        m["path"]
        for m in effective["build"]["metadata"]
        if metadata.sha256[m["path"]] != m["sha256"]
    ]
    if changed:
        raise UsageError(
            f"metadata {', '.join(changed)} changed since compile read it; run compile again, "
            "and the stages after it"
        )
    return metadata


def inputs(build: Build, stage: str) -> tuple[dict, dict[str, dict]]:
    """The effective spec, and the records of the stages `stage` reads, failing unless they fit
    together and the spec and metadata are still those compile read.

    The spec is compiled again rather than read from effective.json, whose JSON holds a TOML date
    as a string; the records' digests make it the spec compile wrote."""
    current, _ = compile_build(build)
    parents = provenance.check(build, stage, current)
    return current, parents


def write_metadata(effective: dict, metadata: Metadata, directory: Path) -> None:
    """The metadata tables the pre-load checks read (metadata.tables), as Parquet."""
    directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for name, rows in metadata_tables(metadata, effective).items():
        con.execute(f"create or replace table t ({SCHEMAS[name]})")
        if rows:
            columns = list(rows[0])
            con.executemany(
                f"insert into t values ({', '.join('?' for _ in columns)})",
                [[r[c] for c in columns] for r in rows],
            )
        con.execute("copy t to ? (format parquet)", [str(directory / f"{name}.parquet")])
