"""Which code, spec and metadata made each output of a build.

Every stage writes a record into output/stage/<build>/provenance.json when it succeeds: the build's
parameters (its area, and digests of the parts of the effective spec and of the metadata), dbgen's
version, git commit and whether its tree had uncommitted changes, the same of the repository that
holds the spec, a run id, and its lineage: the records of the stages whose output it read, directly
or through another stage, as they were when it read them. The record of the last stage so holds
everything that went into what it produced; `dump` copies it into the manifest.

The records also gate the stages: before it starts, a stage checks the stages it reads and refuses
to run unless:

* each ran with the same parameters, as far as they shape its output: a change to a program's
  volumes reruns generate, not bootstrap
* none was rerun after another reader used it: load refuses a generate and a bootstrap that read
  other runs of users, whose users the records name

The stages up to generate and bootstrap remove their record when they start (forget): a run that
fails then leaves the stages after it nothing to read. load removes its record once it has emptied
the tables, so that verify and dump refuse a database whose load failed.

The build database is one Docker volume shared by every build. bootstrap marks it with a comment
on the database holding its run id, and the stages that use the database check the mark.
`pg_dump` without --create leaves database comments out, so the mark stays out of the dump.
"""

import functools
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dbgen.config import DISTRIBUTION, PACKAGE, STAGES, Build, shown
from dbgen.errors import UsageError

# The parts of a build's parameters: its area, and a digest of each part of the effective spec.
AREA = ("codes", "area_name")
PARTS = {
    # the metadata files' bytes, in their order
    "metadata": ("metadata",),
    # what draws the org units: their spec and the seed
    "orgunits": ("orgunits", "seed", "codes"),
    "users": ("users",),
    # what draws the records
    "data": ("programs", "traits", "records", "window"),
    "load": ("load",),
    # what validate and verify leave out of what they submit
    "validation": ("unvalidated",),
}

# The parameters each stage's output depends on.
SHAPED_BY = {
    "compile": (*AREA, *PARTS),
    "orgunits": (*AREA, "orgunits"),
    "users": (*AREA, "orgunits", "users"),
    "bootstrap": (*AREA, "metadata", "orgunits", "users"),
    "generate": (*AREA, "metadata", "orgunits", "users", "data"),
    "load": (*AREA, "metadata", "orgunits", "users", "data", "load"),
    "verify": (*AREA, "metadata", "orgunits", "users", "data", "load", "validation"),
    "validate": (*AREA, "metadata", "orgunits", "users", "data", "validation"),
    "dump": (*AREA, *PARTS),
}

# The stages whose output each stage reads.
PARENT_STAGES = {
    "compile": (),
    "orgunits": ("compile",),
    "users": ("compile", "orgunits"),
    "bootstrap": ("compile", "orgunits", "users"),
    "generate": ("compile", "orgunits", "users"),
    "load": ("generate", "bootstrap"),
    "verify": ("load",),
    "validate": ("generate", "bootstrap"),
    "dump": ("generate", "load", "verify"),
}
assert set(PARENT_STAGES) == {*STAGES, "dump"}


def digests(effective: dict) -> dict[str, str]:
    """A digest of each part of an effective spec (PARTS)."""
    sections = {
        # As the spec names the files, not where they are, which depends on where dbgen runs.
        "metadata": [{k: m[k] for k in ("file", "sha256")} for m in effective["build"]["metadata"]],
        "window": {k: effective["build"][k] for k in ("seed", "start", "end", "now")},
        "seed": effective["build"]["seed"],
        "codes": effective["codes"],
        **{
            k: effective[k]
            for k in (
                "orgunits",
                "users",
                "programs",
                "traits",
                "records",
                "load",
                "unvalidated",
            )
        },
    }
    return {
        part: hashlib.sha256(
            json.dumps([sections[s] for s in keys], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        for part, keys in PARTS.items()
    }


def params(build: Build, effective: dict) -> dict:
    """The parameters of a build as records hold them: what it builds, not where (root)."""
    return {
        "spec": build.spec_name,
        "codes": list(build.codes),
        "area_name": build.area_name,
        **digests(effective),
    }


def check(build: Build, stage: str, effective: dict) -> dict[str, dict]:
    """Return the records of the stages `stage` reads, failing if they do not fit together."""
    code()  # before the stage runs, see code()
    repository(build.spec.resolve().parent)
    records = load(build)
    current = params(build, effective)
    parents = {}
    problems = []
    for parent in PARENT_STAGES[stage]:
        entry = records.get(parent)
        if entry is None:
            problems.append(f"{parent} has not run for this build, run: dbgen stage {parent}")
            continue
        recorded = entry["params"]
        differs = [k for k in SHAPED_BY[parent] if recorded.get(k) != current[k]]
        if differs:
            problems.append(
                f"{parent} ran with another {', '.join(differs)}; rerun it: dbgen stage {parent}"
            )
        parents[parent] = entry
    reads: dict[str, dict[str, str]] = {}
    for parent, entry in parents.items():
        for name, read in lineage(parent, entry).items():
            reads.setdefault(name, {})[parent] = read["run"]
    for name, runs in reads.items():
        # Each stage reads its own part of compile's output, which the digests above check:
        # compile running again for another part leaves it valid.
        if name == "compile":
            continue
        if len(set(runs.values())) > 1:
            latest = records.get(name, {}).get("run")
            stale = [p for p, run in runs.items() if run != latest]
            problems.append(
                f"{name} ran again after {' and '.join(stale)} read it; "
                f"rerun {', then '.join(stale)}"
            )
    if problems:
        raise UsageError(f"{stage}: its inputs do not fit together:\n  " + "\n  ".join(problems))
    return parents


def fresh(build: Build, stage: str, effective: dict) -> bool:
    """Whether the outputs of a stage are those it would write now: it ran with the parameters
    that shape them, and the stages it read are the runs recorded now."""
    records = load(build)
    entry = records.get(stage)
    if entry is None:
        return False
    current = params(build, effective)
    if any(entry["params"].get(k) != current[k] for k in SHAPED_BY[stage]):
        return False
    return all(
        name == "compile" or records.get(name, {}).get("run") == read["run"]
        for name, read in entry["lineage"].items()
    ) and all(fresh(build, parent, effective) for parent in PARENT_STAGES[stage])


def forget(build: Build, stage: str) -> None:
    """Remove the record of a stage that is about to write its outputs again."""
    records = load(build)
    if records.pop(stage, None) is not None:
        _write(build, records)


def record(
    build: Build,
    stage: str,
    parents: dict[str, dict],
    effective: dict,
    run: str | None = None,
    quiet: bool = False,
) -> dict:
    """Write the record of a stage that succeeded, say where its outputs are, and return it."""
    ancestors = {}
    for name, parent in parents.items():
        ancestors.update(lineage(name, parent))
    entry = {
        "run": run or new_run(),
        "finished": datetime.now(UTC).isoformat(timespec="seconds"),
        "params": params(build, effective),
        **code(),
        "spec_repository": repository(build.spec.resolve().parent),
        "lineage": {name: ancestors[name] for name in PARENT_STAGES if name in ancestors},
    }
    records = load(build)
    records[stage] = entry
    _write(build, records)
    if stage != "dump" and not quiet:
        print(f"{stage}: done, {shown(build.stage_dir(stage))}/", file=sys.stderr)
    return entry


def new_run() -> str:
    return uuid.uuid4().hex


@functools.cache
def code() -> dict:
    """dbgen's version, its git commit and whether its tree had uncommitted changes.

    Taken once, when the first stage checks its inputs: dbgen has imported its stages by then, so
    a commit or an edit made while they run does not change the code they are recorded to run.

    Run from its checkout (uv run, an editable install), the commit is the checkout's. Installed
    from git, as the pipeline pins it, it is the commit uv installed, which uv records in the
    package's direct_url.json, without uncommitted changes. Installed otherwise, it is unknown.
    A checkout found around the package must be dbgen's: an install in the .venv of another
    repository would otherwise record that repository's commit.
    """
    version = importlib.metadata.version(DISTRIBUTION)
    top = _git(PACKAGE, "rev-parse", "--show-toplevel")
    if top and Path(top) / "src" / "dbgen" == PACKAGE:
        return {"version": version, **repository(Path(top))}
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        text = None
    commit = json.loads(text).get("vcs_info", {}).get("commit_id") if text else None
    return {"version": version, "commit": commit, "dirty": False if commit else None}


@functools.cache
def repository(directory: Path) -> dict:
    """The git commit of the repository holding a directory, such as a spec's, and whether its
    tree has uncommitted changes; both None outside a repository."""
    commit = _git(directory, "rev-parse", "HEAD")
    status = _git(directory, "status", "--porcelain") if commit else None
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def _git(directory: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def load(build: Build) -> dict[str, dict]:
    path = build.dir / "provenance.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write(build: Build, records: dict[str, dict]) -> None:
    build.dir.mkdir(parents=True, exist_ok=True)
    path = build.dir / "provenance.json"
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    partial.replace(path)


def lineage(stage: str, entry: dict) -> dict[str, dict]:
    """The record of `stage` and of every stage it read, directly or not, by stage."""
    own = {k: v for k, v in entry.items() if k != "lineage"}
    return {**entry["lineage"], stage: own}


def bootstrap_run(entry: dict) -> str:
    """The bootstrap run a record descends from, i.e. the build database it used."""
    return entry["lineage"]["bootstrap"]["run"]


def clean(stage: str, entry: dict) -> bool:
    """Whether every stage of a lineage ran from the same commit of dbgen and of the spec's
    repository, each without uncommitted changes."""
    records = list(lineage(stage, entry).values())
    for state in ([r for r in records], [r.get("spec_repository") or {} for r in records]):
        commits = {s.get("commit") for s in state}
        if None in commits or len(commits) != 1 or any(s.get("dirty") is not False for s in state):
            return False
    return True


def code_change(entry: dict, now: dict) -> str | None:
    """How the code `now` differs from the code a stage ran from, or None if it does not. A
    record says only whether the tree had uncommitted changes, not which, so any is a change."""
    if entry["dirty"]:
        return "ran with uncommitted changes"
    if now["dirty"]:
        return "the code has uncommitted changes now"
    if entry["commit"] is None or entry["commit"] != now["commit"]:
        ran, at = ((c or "unknown")[:7] for c in (entry["commit"], now["commit"]))
        return f"ran from commit {ran}, the code is at {at} now"
    return None


def mark_database(cur, build: Build, run: str) -> None:
    """Mark the build database as the one bootstrap run `run` creates."""
    mark = json.dumps({"stage": "bootstrap", "run": run, "build": build.name})
    cur.execute("comment on database dhis is '{}'".format(mark.replace("'", "''")))


def check_database(cur, bootstrap_run: str) -> None:
    """Fail unless the build database is the one the bootstrap run created."""
    cur.execute(
        "select shobj_description(oid, 'pg_database') from pg_database where datname = 'dhis'"
    )
    (comment,) = cur.fetchone()
    mark = json.loads(comment) if comment else {}
    if mark.get("run") != bootstrap_run:
        found = f"bootstrap run {mark['run']} of {mark.get('build')}" if mark else "no mark"
        raise UsageError(
            f"the build database holds {found}, not bootstrap run {bootstrap_run}; "
            "run bootstrap with --reset again, and the stages after it"
        )
