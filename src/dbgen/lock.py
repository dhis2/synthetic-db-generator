"""lock.toml: the Docker images a build runs; databases/<name>/packages.lock.toml: the published
packages a database's metadata came from.

A build downloads nothing but the images. The packages are pinned so the first commit of each
metadata file can be checked against what DHIS2 publishes (`dbgen lock --check`), and a new
version taken deliberately (`dbgen lock --update <package>`), which extracts it over the metadata
file, so its diff shows what changed and the fixes of `git log --follow <metadata file>` can be
applied again. A package's metadata path is relative to its packages.lock.toml.

lock.toml is dbgen's, shipped with it. The databases are those of the working directory,
databases/<name>/, and git runs in the repository that holds them.
"""

import hashlib
import io
import re
import subprocess
import sys
import tomllib
import zipfile
from datetime import date
from pathlib import Path

from dbgen.config import LOCK, shown
from dbgen.errors import InternalError, UsageError


def images() -> dict[str, str]:
    return dict(tomllib.loads(LOCK.read_text())["images"])


def package_locks() -> list[Path]:
    """The packages.lock.toml of each database of the working directory, databases/<name>/."""
    locks = sorted((Path.cwd() / "databases").glob("*/packages.lock.toml"))
    if not locks:
        raise UsageError(
            "no databases/<name>/packages.lock.toml in the working directory: run dbgen lock in "
            "the repository that holds the databases"
        )
    return locks


def packages(locks: list[Path] | None = None) -> dict[str, dict]:
    """The packages of the given lock files, those of package_locks() by default, by name, each
    with its lock file (lock) and its metadata file resolved against it (path)."""
    out: dict[str, dict] = {}
    for path in package_locks() if locks is None else locks:
        for name, p in tomllib.loads(path.read_text()).get("packages", {}).items():
            if name in out:
                raise UsageError(
                    f"package {name!r} is pinned in {shown(out[name]['lock'])} and {shown(path)}"
                )
            out[name] = {**p, "lock": path, "path": path.parent / p["metadata"]}
    return out


def _first_commit(path: Path) -> tuple[str, str] | None:
    """The commit that added the file, following renames, and the file's path in it, relative
    to the root of its repository."""
    lines = subprocess.run(
        [
            "git",
            "-C",
            str(path.parent),
            "log",
            "--follow",
            "--diff-filter=A",
            "--name-only",
            "--format=%H",
            "--",
            path.name,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return (lines[-2], lines[-1]) if len(lines) >= 2 else None


def _download(url: str) -> bytes:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=300)
    response.raise_for_status()
    return response.content


def _member(archive: bytes, name: str, package: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        try:
            return z.read(name)
        except KeyError as e:
            raise InternalError(f"{package}: the zip holds no {name}") from e


def check(names: list[str]) -> None:
    """Download each package, check its sha256 against the pin, and the file the first commit
    of its metadata file added against the file in the zip."""
    pinned = packages()
    problems = []
    for name in names or list(pinned):
        if name not in pinned:
            raise UsageError(f"no package {name!r} is pinned; the packages are {', '.join(pinned)}")
        p = pinned[name]
        print(f"lock: {name} from {p['url']}", file=sys.stderr)
        archive = _download(p["url"])
        digest = hashlib.sha256(archive).hexdigest()
        if digest != p["sha256"]:
            problems.append(f"{name}: the zip's sha256 is {digest}, pinned {p['sha256']}")
            continue
        published = _member(archive, p["json"], name)
        shown_path = shown(p["path"])
        first = _first_commit(p["path"])
        if first is None:
            problems.append(f"{name}: {shown_path} has no commit")
            continue
        commit, added_as = first
        committed = subprocess.run(
            ["git", "-C", str(p["path"].parent), "show", f"{commit}:{added_as}"],
            capture_output=True,
            check=True,
        ).stdout
        if committed != published:
            problems.append(
                f"{name}: {shown_path} as commit {commit[:10]} added it differs from "
                f"{p['json']} of the zip"
            )
        else:
            print(
                f"lock: {name}: the first commit of {shown_path}, {commit[:10]}, holds the "
                "published file",
                file=sys.stderr,
            )
    if problems:
        raise InternalError("\n  ".join(["the packages do not match their pins:", *problems]))


def update(names: list[str]) -> None:
    """Download each package again, pin its sha256 and extract it over its metadata file."""
    pinned = packages()
    texts: dict[Path, str] = {}
    for name in names:
        if name not in pinned:
            raise UsageError(f"no package {name!r} is pinned; the packages are {', '.join(pinned)}")
        p = pinned[name]
        text = texts.get(p["lock"], p["lock"].read_text())
        archive = _download(p["url"])
        digest = hashlib.sha256(archive).hexdigest()
        p["path"].write_bytes(_member(archive, p["json"], name))
        for field, value in (("sha256", digest), ("retrieved", date.today().isoformat())):
            pattern = re.compile(
                rf'(\[packages\.{re.escape(name)}\][^\[]*?\n{field} = )"[^"]*"', re.DOTALL
            )
            text, count = pattern.subn(rf'\g<1>"{value}"', text)
            if count != 1:
                raise InternalError(f"could not update {field} of {name} in {p['lock']}")
        texts[p["lock"]] = text
        print(
            f"lock: {name} {p['sha256'][:12]} -> {digest[:12]}, extracted to {shown(p['path'])}",
            file=sys.stderr,
        )
    for path, text in texts.items():
        path.write_text(text)
    print(
        "lock: review the diff of the package locks and the metadata, and reapply the fixes of "
        "`git log --follow <metadata file>`",
        file=sys.stderr,
    )
