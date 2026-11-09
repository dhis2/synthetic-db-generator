"""The build's DHIS2 and Postgres: docker compose, the Web API and a Postgres connection."""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg

from dbgen import lock, memory
from dbgen.config import DOCKER
from dbgen.errors import UsageError

COMPOSE_FILE = DOCKER / "compose.yaml"
BASE_URL = "http://127.0.0.1:28080"
ADMIN = ("admin", "district")
PG_DSN = "host=127.0.0.1 port=25432 dbname=dhis user=dhis password=dhis"

# The caps of the stack, which compose.yaml takes, and the memory start leaves free beside it for
# DuckDB and the rest of the machine. An environment variable of the same name replaces a
# default, such as DBGEN_HEADROOM=3g on a smaller machine.
CAPS = {
    "DBGEN_DB_MEMORY": "4g",  # Postgres
    "DBGEN_DB_CPUS": "4",
    "DBGEN_WEB_MEMORY": "5g",  # DHIS2's container
    "DBGEN_WEB_CPUS": "4",
    "DBGEN_WEB_HEAP": "3g",  # DHIS2's Java heap, within its container's memory
    "DBGEN_HEADROOM": "8g",
}
# The cap of each service's memory.
MEMORY = {"db": "DBGEN_DB_MEMORY", "web": "DBGEN_WEB_MEMORY"}
SIZE = re.compile(r"(\d+(?:\.\d+)?)([mg])", re.IGNORECASE)

# Free space the build database needs on Docker's filesystem (2M events take about 5 GiB).
REQUIRED_DISK_GIB = 10


SERVICES = ("db", "web", "web-healthcheck")


@contextmanager
def running(
    project: str, services: tuple[str, ...] = SERVICES, timeout: int = 900
) -> Generator[None]:
    """Start services for the block and stop the stack after it, also when they fail to start:
    `compose up --wait` leaves the services that did start running."""
    try:
        start(project, services, timeout)
        yield
    finally:
        stop(project)


def start(project: str, services: tuple[str, ...] = SERVICES, timeout: int = 900) -> None:
    """Start services, refusing when the machine lacks memory or disk for them.

    Returns once every service is healthy: DHIS2 once it answers /api/ping (web-healthcheck),
    which its request servlet does only after the whole application has started.
    """
    limits = caps()
    needed = sum(gib(limits, MEMORY[s]) for s in services if s in MEMORY)
    needed += gib(limits, "DBGEN_HEADROOM")
    available = memory.available_gib()
    if available < needed:
        raise UsageError(
            f"only {available:.1f} GiB memory available, {' '.join(services)} need "
            f"{needed:g} GiB including headroom. Stop other containers, such as the perf "
            "harness, first, or lower the DBGEN_ caps of the README."
        )
    disk = docker_disk_free_gib()
    if disk < REQUIRED_DISK_GIB:
        raise UsageError(
            f"only {disk:.1f} GiB free where Docker keeps its data, the build database needs "
            f"{REQUIRED_DISK_GIB} GiB. Free some (docker system df shows what uses it)."
        )
    compose(project, "up", "--detach", "--wait", "--wait-timeout", str(timeout), *services)


def stop(project: str, services: tuple[str, ...] = ()) -> None:
    """Stop services, every one of the stack by default; data stays in the pgdata volume."""
    compose(project, "stop", *services)


def reset(project: str) -> None:
    """Remove the build database. Only touches this project's containers and volume."""
    compose(project, "down", "--volumes", "--remove-orphans")


def compose(project: str, *args: str) -> None:
    command, env = compose_command(project, *args)
    print(f"compose {project}: {' '.join(args)}", file=sys.stderr)
    _docker(f"docker compose {' '.join(args)}", command, env)


def compose_command(project: str, *args: str) -> tuple[list[str], dict[str, str]]:
    """The docker compose command line of the project (Build.project) and its environment, with
    the pinned images, <NAME>_IMAGE for each image of lock.toml such as DHIS2_IMAGE, and the
    caps."""
    images = {f"{name.upper()}_IMAGE": image for name, image in lock.images().items()}
    env = {**os.environ, **images, **caps()}
    command = ["docker", "compose", "--file", str(COMPOSE_FILE), "--project-name", project]
    return [*command, *args], env


def caps() -> dict[str, str]:
    """The caps of CAPS, each from the environment variable of its name if set, checked."""
    limits = {name: os.environ.get(name, default).strip() for name, default in CAPS.items()}
    for name, value in limits.items():
        if name.endswith("_CPUS"):
            try:
                ok = float(value) > 0
            except ValueError:
                ok = False
            if not ok:
                raise UsageError(f"{name}={value!r}: give a number of CPUs, such as 2 or 1.5")
        else:
            gib(limits, name)
    if gib(limits, "DBGEN_WEB_HEAP") >= gib(limits, "DBGEN_WEB_MEMORY"):
        raise UsageError(
            f"DBGEN_WEB_HEAP={limits['DBGEN_WEB_HEAP']} must be less than DBGEN_WEB_MEMORY="
            f"{limits['DBGEN_WEB_MEMORY']}, which holds the heap and the rest of the JVM"
        )
    return limits


def gib(limits: dict[str, str], name: str) -> float:
    """A memory cap in GiB, given as compose takes it, such as 4g or 3584m."""
    match = SIZE.fullmatch(limits[name])
    if not match:
        raise UsageError(f"{name}={limits[name]!r}: give a size such as 4g or 3584m")
    number, unit = match.groups()
    return float(number) / (1024 if unit.lower() == "m" else 1)


def image_labels(image: str) -> dict[str, str]:
    """The labels of a local image, e.g. DHIS2_VERSION and DHIS2_BUILD_REVISION of DHIS2's."""
    command = ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image]
    return json.loads(_docker("docker image inspect", command, capture=True)) or {}


def docker_disk_free_gib() -> float:
    """Free space of the filesystem holding Docker's data (volumes, images, containers)."""
    command = ["docker", "info", "--format", "{{.DockerRootDir}}"]
    path = Path(_docker("docker info", command, capture=True))
    while True:  # the root dir itself is usually not readable by the user; its parent is
        try:
            return shutil.disk_usage(path).free / (1 << 30)
        except PermissionError:
            path = path.parent


def _docker(
    name: str, command: list[str], env: dict[str, str] | None = None, capture: bool = False
) -> str:
    """Run a docker command, named `name` in its error; its output, stripped, when captured.

    Fails with a UsageError: docker missing or not running, a port taken or a service that does
    not come up are the machine's, and the error says which.
    """
    try:
        result = subprocess.run(command, env=env, capture_output=capture, text=True)
    except FileNotFoundError as e:
        raise UsageError("docker is not installed, or not on PATH") from e
    if result.returncode != 0:
        error = result.stderr.strip() if capture else "its error is above"
        raise UsageError(f"{name} failed with exit code {result.returncode}: {error}")
    return result.stdout.strip() if capture else ""


def api(timeout: float = 600) -> httpx.Client:
    """The Web API, as admin. A context manager, which closes it."""
    return httpx.Client(base_url=BASE_URL, auth=ADMIN, timeout=timeout)


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(PG_DSN, **kwargs)
