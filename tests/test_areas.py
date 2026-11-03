"""dbgen areas: an org unit and those below it, with the records explain expects of each."""

import pytest

from dbgen import areas, explain
from dbgen import compile as compile_stage
from dbgen.config import Build
from dbgen.errors import UsageError
from dbgen.orgunits import build_orgunits
from tests.conftest import write_spec

# Tracked entities per facility, and children linked from the pregnancies.
PROGRAMS = """
[trait.facility]
size = { lognormal = { median = 1, sigma = 0.6 } }

[program.PREGNANCY]
tracked_entities = { per_orgunit = 20, by = "facility.size" }
enrollments = { count = { poisson = 0.5, plus = 1 } }

[program.CHILD]
tracked_entities = 2

[program.CHILD.from]
enrollments = { of = "PREGNANCY", as = "mother" }
where = "mother.number = 0"
share = 0.5
relationship = "Mother and child"
at = "mother.end + 3"
"""


@pytest.fixture
def spec(tmp_path):
    return write_spec(tmp_path, PROGRAMS)


def explained(spec, codes: tuple[str, ...] = ()) -> dict:
    """The tracked entities and events explain expects of a build of the codes."""
    effective, _ = compile_stage.compile_build(Build(spec=spec, codes=codes, root=spec.parent))
    expected = explain._expected(build_orgunits(effective), effective)
    return {
        "tracked_entities": sum(e["tracked_entities"] for e in expected.values()),
        "events": sum(e["events"] for e in expected.values()),
    }


def test_the_root_and_its_children_hold_what_explain_expects(spec):
    rows = areas.areas(Build(spec=spec, root=spec.parent))
    assert [(r["code"], r["level"], r["children"]) for r in rows] == [
        ("QP", "Perf Land", 2),
        ("QP01", "Region", 1),
        ("QP02", "Region", 1),
    ]
    root, *regions = rows
    whole = explained(spec)
    assert whole["events"] > 0
    for key in ("tracked_entities", "events"):
        assert root[key] == pytest.approx(whole[key])
        assert sum(r[key] for r in regions) == pytest.approx(root[key])
    for r in regions:
        assert r["tracked_entities"] == pytest.approx(
            explained(spec, (r["code"],))["tracked_entities"]
        )


def test_a_facility_is_listed_alone_with_its_traits(spec):
    _, district = areas.areas(Build(spec=spec, root=spec.parent), "QP01")
    facility = areas.areas(Build(spec=spec, root=spec.parent), f"{district['code']}-01")
    assert len(facility) == 1
    assert facility[0]["children"] == 0
    assert set(facility[0]["traits"]) == {"size"}
    assert facility[0]["tracked_entities"] > 0


def test_a_code_the_spec_does_not_draw_fails(spec):
    with pytest.raises(UsageError, match="draws no org unit QP99"):
        areas.areas(Build(spec=spec, root=spec.parent), "QP99")


def test_run_prints_a_row_per_org_unit(spec, capsys):
    areas.run(Build(spec=spec, root=spec.parent))
    out = capsys.readouterr().out.splitlines()
    header, *rows, _, _, build = out
    assert "--orgunit QP0" in build
    assert header.split()[:4] == ["code", "level", "name", "children"]
    assert [row.split()[0] for row in rows] == ["QP", "QP01", "QP02"]


def test_run_suggests_building_the_org_unit_it_lists(spec, capsys):
    areas.run(Build(spec=spec, root=spec.parent), "QP01")
    build = capsys.readouterr().out.splitlines()[-1]
    assert build.endswith("--orgunit QP01")
