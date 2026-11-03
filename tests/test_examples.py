"""The examples compile, so the README's commands keep working."""

from pathlib import Path

import pytest

from dbgen import compile as compile_stage
from dbgen.config import Build

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*/*.toml"))


@pytest.mark.parametrize("spec", EXAMPLES, ids=lambda spec: spec.stem)
def test_the_example_compiles(tmp_path, spec):
    effective = compile_stage.compile_build(Build(spec=spec, root=tmp_path))[0]
    assert effective["programs"]
