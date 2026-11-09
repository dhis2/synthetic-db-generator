"""`dbgen stage dump`: the plain SQL dump the perf harness restores, and its manifest.

output/<build>/dhis2-db-<build>.sql.gz is `pg_dump --no-owner --no-privileges`, gzipped, the
format core's dhis-test-performance/docker/Dockerfile.postgres restores with psql.

output/<build>/manifest.json records what the dump was built from, among it the DHIS2 version and
core commit of the DHIS2 image, and the row digests of the org units, the users and every tracker
table that `generate` wrote (generate/checksums.json, see digest.py). `dump` checks that the
database holds exactly as many tracker rows as generate wrote. Its provenance is the lineage
of every stage, with the git commit each ran from (see provenance.py); `clean` says whether they
all ran from one commit without uncommitted changes.

The bytes of the dump file differ between builds all the same: it holds the times bootstrap
imported the metadata and verify logged in, the users' random UUIDs and password salts, and the
tracker rows in the order load's parallel streams wrote them.
"""

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import IO

from isal import igzip

from dbgen import compile as compile_stage
from dbgen import lock, provenance, timing
from dbgen.config import Build, shown
from dbgen.dhis2 import client
from dbgen.errors import InternalError, UsageError
from dbgen.generate import TABLES


def run(build: Build) -> Path:
    effective, parent_stages = compile_stage.inputs(build, "dump")
    target = build.output / dump_name(build)
    manifest_file = build.output / "manifest.json"
    checksums = json.loads((build.stage_dir("generate") / "checksums.json").read_text())
    dhis2 = _dhis2_labels()
    # The old manifest describes the dump about to be replaced: a failure before the new one is
    # written leaves no manifest rather than one that does not match the dump.
    manifest_file.unlink(missing_ok=True)
    with timing.timed(build.dir, "dump") as t:
        with client.running(build.project, ("db",), timeout=300):
            with client.connect(autocommit=True) as pg, pg.cursor() as cur:
                provenance.check_database(cur, provenance.bootstrap_run(parent_stages["load"]))
                check_row_counts(cur, checksums)
                cur.execute(
                    "select version from flyway_schema_history order by installed_rank desc limit 1"
                )
                [(flyway,)] = cur.fetchall()
            sha256 = write_dump(build.project, target)
        size = target.stat().st_size
        t.add(bytes_out=size)

    verify = build.stage_dir("verify") / "results.json"
    load_steps = build.stage_dir("load") / "steps.json"
    entry = provenance.record(build, "dump", parent_stages, effective)
    manifest = {
        "dump": {"file": target.name, "sha256": sha256, "bytes": size},
        "build": entry["params"],
        "provenance": {
            "version": entry["version"],
            "commit": entry["commit"],
            "dirty": entry["dirty"],
            "spec_repository": entry["spec_repository"],
            "clean": provenance.clean("dump", entry),
            "lineage": provenance.lineage("dump", entry),
        },
        "images": lock.images(),
        "dhis2": dhis2,
        "flyway_version": flyway,
        "versions": _versions(),
        "spec": effective,
        "metadata": {m["file"]: m["sha256"] for m in effective["build"]["metadata"]},
        "packages": _packages(build),
        "tables": checksums,
        "verify": json.loads(verify.read_text()) if verify.exists() else None,
        "timings": timing.summary(build.dir, provenance.lineage("dump", entry)),
        "load_steps": json.loads(load_steps.read_text()) if load_steps.exists() else None,
    }
    partial = manifest_file.with_name(manifest_file.name + ".part")
    partial.write_text(json.dumps(manifest, indent=2) + "\n")
    partial.replace(manifest_file)
    size_mib = size / (1 << 20)
    print(
        f"dump: done, {shown(target)} ({size_mib:,.0f} MiB) and its manifest.json", file=sys.stderr
    )
    return target


def dump_name(build: Build) -> str:
    """e.g. dhis2-db-mnch-QP1613.sql.gz: named after the build, as the harness names its
    dumps after their database type (dhis2-db-sierra-leone.sql.gz)."""
    return f"dhis2-db-{build.name}.sql.gz"


def check_row_counts(cur, expected: dict[str, dict]) -> None:
    """The database must hold exactly the rows generate wrote."""
    mismatches = []
    for table in TABLES:
        cur.execute(f"select count(*) from {table}")
        (rows,) = cur.fetchone()
        if rows != expected[table]["rows"]:
            mismatches.append(f"{table}: {rows} rows loaded, {expected[table]['rows']} generated")
    if mismatches:
        raise InternalError(
            "the build database holds other rows than generate wrote:\n  " + "\n  ".join(mismatches)
        )


def write_dump(project: str, target: Path) -> str:
    """Stream pg_dump from the db container into a gzip file; return its sha256."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    command, env = client.compose_command(
        project,
        "exec",
        "-T",
        "db",
        "pg_dump",
        "--no-owner",
        "--no-privileges",
        "--username=dhis",
        "dhis",
    )
    with subprocess.Popen(command, stdout=subprocess.PIPE, env=env) as proc:
        stdout = proc.stdout
        if stdout is None:
            raise RuntimeError("pg_dump has no stdout pipe")
        sha256 = gzip_to(stdout, partial)
    if proc.returncode != 0:
        raise UsageError(f"pg_dump failed with exit code {proc.returncode}, its error is above")
    partial.replace(target)
    return sha256


def gzip_to(src: IO[bytes], target: Path) -> str:
    """Gzip src into target; return the sha256 of the gzip file, taken while writing it."""
    digest = hashlib.sha256()
    with (
        target.open("wb") as raw,
        # ISA-L gzip at level 1 compresses 13 times faster than zlib at level 6 (1.8 GiB/s against
        # 140 MiB/s) for an 18% larger file, so pg_dump sets the pace. Output is plain gzip. No
        # file name in the header: it would be the .part name, which gunzip --name restores.
        igzip.GzipFile(
            filename="",
            fileobj=_HashingWriter(raw, digest),
            mode="wb",
            compresslevel=1,
            mtime=0,
        ) as out,
    ):
        while chunk := src.read(1 << 20):
            out.write(chunk)
    return digest.hexdigest()


class _HashingWriter:
    """Writes to a file and hashes what it writes, so the file need not be read again. The gzip
    writer only appends, so the hash covers the file's bytes in order."""

    def __init__(self, f: IO[bytes], digest: "hashlib._Hash"):
        self._f = f
        self._digest = digest

    def write(self, b) -> int:
        self._digest.update(b)
        return self._f.write(b)

    def flush(self) -> None:
        self._f.flush()


def _dhis2_labels() -> dict[str, str]:
    """The DHIS2_ labels of the pinned DHIS2 image (DHIS2_VERSION, DHIS2_BUILD_REVISION, ...): its
    digest does not say which core commit it is. Core's run-simulation.sh records the same."""
    labels = client.image_labels(lock.images()["dhis2"])
    return {k: v for k, v in sorted(labels.items()) if k.startswith("DHIS2_")}


def _packages(build: Build) -> dict[str, str]:
    """The sha256 of each package pinned beside the spec, in its packages.lock.toml."""
    path = build.spec.parent / "packages.lock.toml"
    return {name: p["sha256"] for name, p in lock.packages([path]).items()} if path.exists() else {}


def _versions() -> dict:
    # Not dbgen's own version: the provenance records it with the commit. DuckDB's draws every
    # value (sql/macros.sql), so its version is part of what makes the rows the same.
    packages = ["duckdb", "pyarrow", "numpy", "psycopg"]
    return {
        "python": platform.python_version(),
        **{p: importlib.metadata.version(p) for p in packages},
    }
