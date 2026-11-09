import re
from pathlib import Path

from dbgen.config import Build


def project(root: Path) -> str:
    return Build(spec=Path("example.toml"), root=root).project


def test_project_is_the_same_for_a_root(tmp_path):
    assert project(tmp_path) == project(tmp_path / "." / "")


def test_project_differs_for_roots_of_the_same_name(tmp_path):
    a, b = tmp_path / "a" / "checkout", tmp_path / "b" / "checkout"
    assert project(a) != project(b)
    assert project(a).startswith("dbgen-checkout-")


def test_project_is_a_name_compose_takes(tmp_path):
    name = project(tmp_path / "My Checkout.v2_")
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name), name
    assert name.startswith("dbgen-my-checkout-v2-")
