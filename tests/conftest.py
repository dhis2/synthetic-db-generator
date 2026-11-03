from pathlib import Path

import pytest

from dbgen.config import Build

# A made-up metadata export: a Pregnancy and a Child program, linked by Mother and child, with
# the value types, option sets, stages and attributes the tests need.
METADATA = [str(Path(__file__).parent / "fixtures" / "metadata.json")]


def write_spec(directory: Path, body: str = "", name: str = "tiny") -> Path:
    """A spec over the fixture metadata, of two regions of one district and two facilities
    each, and the given tables."""
    path = directory / f"{name}.toml"
    path.write_text(
        f"""
[build]
metadata = {METADATA!r}
seed = 7
start = 2023-01-01
end = 2025-01-01

[orgunits]
root = {{ name = "Perf Land", code = "QP" }}

[[orgunits.level]]
name = "Region"
count = 2

[[orgunits.level]]
name = "District"
per_parent = 1

[[orgunits.level]]
name = "Facility"
per_parent = 2
{body}
"""
    )
    return path


@pytest.fixture
def tiny(tmp_path) -> Build:
    return Build(spec=write_spec(tmp_path), root=tmp_path)
