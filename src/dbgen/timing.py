"""Per-stage timings.

Each stage writes one record into output/stage/<build>/timings/<stage>.json. Timings are kept
apart from the pipeline outputs, since wall times differ between runs and the outputs must not.
"""

import json
import resource
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Timing:
    stage: str
    # The enclosed block raised, so the stage did not finish.
    failed: bool = False
    wall_seconds: float = 0.0
    # User and system CPU time of this process.
    cpu_seconds: float = 0.0
    # CPU time of the subprocesses waited for, such as docker compose; not the containers'.
    child_cpu_seconds: float = 0.0
    # The highest RSS during the stage, counting what the process held when it began.
    peak_rss_mib: float = 0.0
    # Each stage's own counts, such as rows_<table> or bytes_out, summed over add calls.
    counters: dict[str, int] = field(default_factory=dict)

    def add(self, **counters: int) -> None:
        """Add to counters such as rows_out=..., bytes_out=..."""
        for name, value in counters.items():
            self.counters[name] = self.counters.get(name, 0) + int(value)


@contextmanager
def timed(build_dir: Path, stage: str) -> Generator[Timing]:
    """Measure the enclosed block and write its Timing, also when the block fails, so that the
    record of a failed run replaces that of the run before it and says it failed."""
    timing = Timing(stage=stage)
    _reset_peak_rss()
    wall_start = time.perf_counter()
    cpu_start, child_start = _cpu()
    try:
        yield timing
    except BaseException:
        timing.failed = True
        raise
    finally:
        cpu_end, child_end = _cpu()
        timing.wall_seconds = round(time.perf_counter() - wall_start, 3)
        timing.cpu_seconds = round(cpu_end - cpu_start, 3)
        timing.child_cpu_seconds = round(child_end - child_start, 3)
        timing.peak_rss_mib = _peak_rss_mib()
        write(build_dir, timing)


def _reset_peak_rss() -> None:
    """Lower the process's peak RSS to its RSS now (Linux, proc(5) clear_refs), so that a stage
    run after others in one process, as `dbgen build` runs them, reports its own peak instead of
    the largest before it. A forked worker's RSS still counts what it shares with its parent."""
    with suppress(OSError):
        Path("/proc/self/clear_refs").write_text("5")


def _cpu() -> tuple[float, float]:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime


def _peak_rss_mib() -> float:
    """The peak RSS since _reset_peak_rss, or where /proc is missing since the process started.

    The fallback reads ru_maxrss as KiB, which holds on Linux only: macOS gives bytes, so there
    the figure is 1024 times too large."""
    with suppress(OSError):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024, 1)
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def write(build_dir: Path, timing: Timing) -> Path:
    directory = build_dir / "timings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timing.stage}.json"
    path.write_text(json.dumps(asdict(timing), indent=2, sort_keys=True) + "\n")
    return path


def summary(build_dir: Path, stages: Iterable[str]) -> list[dict]:
    """The timings of the stages that recorded one, sorted by stage, for the manifest: those of a
    dump's lineage, so not those of a stage, such as validate, that ran beside it."""
    paths = sorted(build_dir / "timings" / f"{stage}.json" for stage in stages)
    return [json.loads(p.read_text()) for p in paths if p.exists()]
