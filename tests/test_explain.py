"""dbgen explain: the effective spec as an overlay of the metadata."""

import re

from dbgen import explain, spec
from dbgen.config import Build
from tests.conftest import write_spec

# A spec that sets every key explain shows, each away from its default.
EVERY_KEY = """
[users]
prefix = "every"
password = "Every-Key-2026!"
clerks = 2
officers = ["Region"]
search_level = "Region"

[records]
updated = { share = 0.1, after = 5 }

[load]
order = "parallel"

[program.PREGNANCY]
orgunit_level = "Facility"
tracked_entities = { total = 50, among = "District", by = "facility.weight" }
unvalidated = ["HB"]

[program.PREGNANCY.enrollments]
count = 1
gap = 30
status = { ACTIVE = 1 }
followup = 0.1

[program.PREGNANCY.visits]
stages = ["Lab"]
count = 2
spacing = 30
status = { COMPLETED = 1 }

[program.PREGNANCY.stage.Check]
at = "end"
offset = 1
share = 0.5
count = 2
spacing = 7
status = { COMPLETED = 1 }
assigned = 0.5

[program.PREGNANCY.values]
HB = 12

[program.PREGNANCY.attributes]
FAMILY_NAME = { words = { n = 100 } }

[program.CHILD]
tracked_entities = { per_orgunit = 0 }

[program.CHILD.from]
enrollments = { of = "PREGNANCY", as = "mother" }
where = "mother.number = 0"
share = 0.9
relationship = "Mother and child"
at = "mother.end + 3"
attributes = { FAMILY_NAME = "mother.FAMILY_NAME" }
"""


def explained(tmp_path, capsys, body: str) -> str:
    explain.run(Build(spec=write_spec(tmp_path, body), root=tmp_path))
    return capsys.readouterr().out


def test_explain_shows_every_key_a_spec_sets(tmp_path, capsys):
    out = explained(tmp_path, capsys, EVERY_KEY)
    # A key heads its line, its value after it or below it when both do not fit, or follows another
    # of its block on the line of the metadata's or the defaults; a table's own keys are in its
    # value, such as { share = 0.1, after = 5 }.
    tables = ["build", "users", "load", "program", "enrollments", "visits", "stage", "from"]
    for table in tables:
        # A program's traits are the spec's own, named by its expressions.
        for key in spec.KEYS[table] - {"trait"}:
            assert re.search(rf"(^ *|: |, ){key}([ :,]|$)", out, re.MULTILINE), f"{table}.{key}"
    for table in ("records", "updated", "tracked_entities"):
        for key in spec.KEYS[table]:
            assert re.search(rf"\b{key}\b", out), f"{table}.{key}"


def test_explain_marks_what_the_metadata_gives_and_what_the_spec_sets_over_it(tmp_path, capsys):
    # Check repeats every 28 days by its standardInterval, Lab has none.
    out = explained(tmp_path, capsys, "")
    assert "metadata: spacing 28 days (standardInterval)" in out
    lab = out[out.index("stage Lab") : out.index("stage Close")]
    assert re.search(r"default: .*\n? *spacing 30 days,", lab)
    out = explained(tmp_path, capsys, "[program.PREGNANCY.stage.Check]\nspacing = 7")
    assert "    spacing 7 days [over standardInterval 28]\n" in out


def test_explain_marks_a_field_only_where_the_metadata_gives_its_generator(tmp_path, capsys):
    out = explained(tmp_path, capsys, "[program.PREGNANCY.values]\nHB = 12")
    assert re.search(r"^ +HB .*: 12$", out, re.MULTILINE)
    assert re.search(r"^ +LMP .*\[default\]$", out, re.MULTILINE)
