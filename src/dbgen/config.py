"""What to build and where its outputs go.

A build is a spec (spec.py) and the area it covers: every org unit under the codes of --orgunit,
or all of them. Its name is the spec's and the codes', such as mnch-QP1613, and names its
directories:

output/                       what builds write, under the build's root (the working directory)
├── stage/<build>/            the outputs of the stages of one build
│   ├── <stage>/              the outputs of each stage, e.g. compile/, generate/, load/
│   ├── timings/              the timings of generate and the stages after it (timing.py)
│   └── provenance.json       what each stage ran with and read (provenance.py)
└── <build>/                  dump: the SQL dump and its manifest
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# dbgen's own files, which a release ships as package data: the images it builds with
# (lock.toml) and their compose setup (docker/).
# The distribution that installs the package dbgen, as pyproject.toml names it.
DISTRIBUTION = "synthetic-db-generator"
PACKAGE = Path(__file__).resolve().parent
LOCK = PACKAGE / "lock.toml"
DOCKER = PACKAGE / "docker"

# The stages with a directory in output/stage/<build>/, in the order `build` runs them, and
# validate, which `dbgen validate` runs after generate and bootstrap. dump writes to
# output/<build>/.
STAGES = (
    "compile",
    "orgunits",
    "users",
    "generate",
    "bootstrap",
    "load",
    "verify",
    "validate",
)

# The name of a build: lowercase words joined by '-', as the spec's file is named.
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# An org unit code, as a build's directory name holds it.
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def listed(counts: dict, unit: str = "") -> str:
    """Names and their numbers to print, such as `clerk 3, admin 1`, or `load 1.2s` with unit s."""
    return ", ".join(
        f"{name} {n:,.1f}{unit}" if isinstance(n, float) else f"{name} {n:,}{unit}"
        for name, n in counts.items()
    )


def shown(path: Path) -> str:
    """A path to print: relative to the current directory when it lies inside it."""
    return str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)


@dataclass(frozen=True)
class Build:
    """What to build: a spec, over the org units under the given codes, or all of them."""

    spec: Path
    codes: tuple[str, ...] = ()  # e.g. ("QP1613",); none covers every org unit
    area_name: str | None = None  # names the area of several codes, e.g. large
    root: Path = field(default_factory=Path.cwd)  # holds output/

    def __post_init__(self) -> None:
        if self.spec.is_dir():
            specs = sorted(str(p) for p in self.spec.glob("*.toml") if NAME.fullmatch(p.stem))
            hint = f", such as {', '.join(specs)}" if specs else ""
            raise ValueError(f"spec {self.spec} is a directory: a spec is a .toml file{hint}")
        invalid = [code for code in self.codes if not CODE.fullmatch(code)]
        if invalid:
            raise ValueError(
                f"--orgunit {', '.join(map(repr, invalid))}: an org unit code is letters, digits, "
                "_ and -, such as QP01"
            )
        if len(self.codes) > 1 and not self.area_name:
            raise ValueError("--orgunit with several codes needs --name, such as --name large")
        if len(self.codes) <= 1 and self.area_name:
            raise ValueError("--name needs several --orgunit codes; one code names the build")
        if self.area_name and not NAME.fullmatch(self.area_name):
            raise ValueError(
                f"--name {self.area_name!r}: use lowercase words joined by '-', such as large"
            )
        if not NAME.fullmatch(self.spec_name):
            raise ValueError(
                f"spec file {self.spec.name!r}: its name must be lowercase words joined by '-', "
                "as it names the build"
            )

    @property
    def spec_name(self) -> str:
        return self.spec.stem

    @property
    def name(self) -> str:
        """e.g. mnch, mnch-QP1613, mnch-large: the spec's name and the area's."""
        area = self.area_name or (self.codes[0] if self.codes else None)
        return f"{self.spec_name}-{area}" if area else self.spec_name

    @property
    def dir(self) -> Path:
        """The build's directory in output/stage/: its stage outputs, timings and provenance."""
        return self.root / "output" / "stage" / self.name

    def stage_dir(self, stage: str) -> Path:
        """The directory holding the outputs of a stage, e.g. output/stage/<build>/load."""
        if stage not in STAGES:
            raise ValueError(f"stage {stage!r} has no directory in output/stage/")
        return self.dir / stage

    def make_stage_dir(self, stage: str) -> Path:
        """The directory of a stage's outputs, created if missing: for the stage writing them."""
        directory = self.stage_dir(stage)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def project(self) -> str:
        """The compose project of the build database, one per root, such as
        dbgen-synthetic-db-generator-3f9a1c: builds of two checkouts never share its containers or
        volume. The hash of the root keeps apart two that share a directory name."""
        root = self.root.resolve()
        name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:6]
        return "-".join(part for part in ("dbgen", name, digest) if part)

    @property
    def output(self) -> Path:
        """The outputs of `dump`: the SQL dump and its manifest."""
        return self.root / "output" / self.name
