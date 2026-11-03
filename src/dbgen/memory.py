"""The memory the machine has left, by which simulate sizes its workers and bootstrap and verify
check that DHIS2 and Postgres fit."""

from pathlib import Path


def available_gib() -> float:
    """Memory available to new processes without swapping, MemAvailable of /proc/meminfo, in GiB."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise RuntimeError("MemAvailable missing from /proc/meminfo")
