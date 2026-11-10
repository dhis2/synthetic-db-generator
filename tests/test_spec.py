import json
import re

import duckdb
import pytest

from dbgen import compile as compile_stage
from dbgen import generate, provenance, spec
from dbgen.config import Build
from dbgen.errors import UsageError
from dbgen.metadata import Field
from tests.conftest import METADATA, write_spec


def effective(tmp_path, body: str):
    return compile_stage.compile_build(Build(spec=write_spec(tmp_path, body), root=tmp_path))[0]


def program(eff, key):
    return next(p for p in eff["programs"] if p["key"] == key)


def test_every_program_gets_its_defaults(tmp_path):
    eff = effective(tmp_path, "")
    assert {p["uid"] for p in eff["programs"]} == {"PregProgram", "ChildProgra"}
    preg = next(p for p in eff["programs"] if p["uid"] == "PregProgram")
    values = [v for s in preg["stages"] for v in s["values"]]
    assert all(v["source"] == "default" for v in values)


@pytest.mark.parametrize(
    "bodies",
    [
        [f"[program.{p}.values]\nHB = 12" for p in ("PREGNANCY", "PregProgram", "pregnancy")],
        [f"[program.PREGNANCY.values]\n{f} = 12" for f in ("HB", "Haemoglobin", '"haemoglobin"')],
    ],
    ids=["program", "field"],
)
def test_a_program_or_field_by_its_code_uid_or_name_compiles_the_same(tmp_path, bodies):
    """The same compiled spec, of which generate draws the same rows: but for the spec's path and
    the names it gives its values, which explain shows and generate does not read."""
    compiled = []
    for i, body in enumerate(bodies):
        directory = tmp_path / str(i)
        directory.mkdir()
        eff = effective(directory, body)
        del eff["spec"]
        for p in eff["programs"]:
            for s in p["stages"]:
                for v in s["values"]:
                    del v["key"]
        compiled.append(eff)
    assert compiled[0] == compiled[1] == compiled[2]
    values = {
        v["code"]: v for s in program(compiled[0], "PREGNANCY")["stages"] for v in s["values"]
    }
    assert (values["HB"]["source"], values["HB"]["gen"]) == ("spec", 12)


def test_a_value_names_its_field_by_code(tmp_path):
    eff = effective(
        tmp_path,
        """
[program.PREGNANCY]

[program.PREGNANCY.values]
"HB" = { normal = { mean = 11.5, sd = 1.5 }, min = 4, max = 18 }
""",
    )
    [hb] = [
        v for s in program(eff, "PREGNANCY")["stages"] for v in s["values"] if v["code"] == "HB"
    ]
    assert hb["source"] == "spec"
    assert hb["gen"]["normal"] == {"mean": 11.5, "sd": 1.5}


def test_a_field_the_spec_leaves_alone_is_empty_unless_compulsory(tmp_path):
    eff = effective(tmp_path, "[program.PREGNANCY]")
    values = {v["code"]: v for s in program(eff, "PREGNANCY")["stages"] for v in s["values"]}
    assert values["SYSTOLIC"]["required"]
    assert values["SYSTOLIC"]["gen"] is not None and values["SYSTOLIC"]["fill"] == 1
    assert not values["NOTES"]["required"]
    assert values["NOTES"]["gen"] is None


def test_a_value_the_spec_sets_is_written_always_unless_it_gives_its_fill(tmp_path):
    eff = effective(
        tmp_path,
        """
[program.PREGNANCY]

[program.PREGNANCY.values]
NOTES = "seen"
HB = { constant = 12, fill = 0.4 }
""",
    )
    values = {v["code"]: v for s in program(eff, "PREGNANCY")["stages"] for v in s["values"]}
    assert (values["NOTES"]["gen"], values["NOTES"]["fill"]) == ("seen", 1)
    assert (values["HB"]["gen"], values["HB"]["fill"]) == ({"constant": 12}, 0.4)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('[program.NOPE]', "no tracker program 'NOPE'"),
        # A UID matches exactly, as DHIS2 tells UIDs apart by case.
        ("[program.pregprogram]", "no tracker program 'pregprogram'"),
        ('[program.PREGNANCY]\n[program.PREGNANCY.values]\nNOPE = 1',
         "program.PREGNANCY.values: 'NOPE' is not a data element of the program"),
        ('[program.PREGNANCY.attributes]\nNOPE = 1',
         "program.PREGNANCY.attributes: 'NOPE' is not an attribute of the program"),
        ('[program.PREGNANCY.values]\nGIVEN_NAME = "Ada"',
         r"program.PREGNANCY.values.GIVEN_NAME: an attribute; set it in "
         r"\[program.PREGNANCY.attributes\]"),
        ('[program.PREGNANCY.attributes]\nHB = 12',
         r"program.PREGNANCY.attributes.HB: a data element; set it in "
         r"\[program.PREGNANCY.values\]"),
        ('[program.PREGNANCY]\n[program.PREGNANCY.stage."Nope"]\nshare = 1',
         "no stage 'Nope'"),
        ('[program.PREGNANCY]\nvisits = { stages = ["Check"], '
         'status = { DONE = 1 } }', "unknown status DONE"),
    ],
)  # fmt: skip
def test_a_name_the_metadata_does_not_hold_fails(tmp_path, body, error):
    with pytest.raises(UsageError, match=error):
        effective(tmp_path, body)


def test_a_window_without_an_end_after_its_start_fails(tmp_path):
    spec = tmp_path / "window.toml"
    spec.write_text(f"[build]\nmetadata = {METADATA!r}\nstart = 2024-01-01\nend = 2024-01-01\n")
    with pytest.raises(UsageError, match="build.end: must be after"):
        compile_stage.compile_build(Build(spec=spec, root=tmp_path))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2021-03-01", "2026-03-01"),
        ("2021-01-31", "2026-01-31"),
        ("2020-02-29", "2025-02-28"),
    ],
)
def test_the_window_ends_after_five_years_without_an_end(tmp_path, start, end):
    spec = tmp_path / "window.toml"
    spec.write_text(f"[build]\nmetadata = {METADATA!r}\nstart = {start}\n")
    eff = compile_stage.compile_build(Build(spec=spec, root=tmp_path))[0]
    assert (eff["build"]["start"], eff["build"]["end"]) == (start, end)


def test_a_name_of_several_fields_fails():
    fields = [
        Field(
            uid="Weight00001", code="WEIGHT", name="Weight", value_type="NUMBER", option_set=None
        ),
        Field(
            uid="Weight00002", code="WEIGHT_KG", name="weight", value_type="NUMBER", option_set=None
        ),
    ]
    assert spec.find_field("WEIGHT_KG", fields, "where").uid == "Weight00002"
    with pytest.raises(UsageError, match="'Weight' names several fields"):
        spec.find_field("Weight", fields, "program.ANC.values.Weight")


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ('"PREGNANCY"', 'must be { of = "<code>", as = "<name>" }'),
        ('{ of = "PREGNANCY" }', 'must be { of = "<code>", as = "<name>" }'),
        ('{ of = "PREGNANCY", as = "event" }', "'event' is a level or a scope already"),
        ('{ of = "PREGNANCY", as = "district" }', "'district' is a level or a scope already"),
        ('{ of = "PREGNANCY", as = "Mother" }', "lowercase letters"),
    ],
)
def test_a_linked_program_names_its_source(tmp_path, source, error):
    with pytest.raises(UsageError, match=re.escape(error)):
        effective(tmp_path, f"[program.CHILD]\nfrom = {{ enrollments = {source} }}")


def test_a_linked_programs_source_goes_by_the_name_it_gives(tmp_path):
    eff = effective(
        tmp_path,
        """
[program.PREGNANCY]

[program.CHILD]

[program.CHILD.from]
enrollments = { of = "PREGNANCY", as = "pregnancy" }
attributes = { FAMILY_NAME = "pregnancy.FAMILY_NAME" }
""",
    )
    f = program(eff, "CHILD")["from"]
    assert (f["enrollments"], f["as"], f["at"]) == ("PREGNANCY", "pregnancy", "pregnancy.end")
    assert list(f["attributes"].values()) == [{"from": "FamilyName1"}]


@pytest.mark.parametrize("copied", ['"pregnancy.end + 7"', '"upper(pregnancy.FAMILY_NAME)"', "7"])
def test_a_linked_program_copies_attributes_only(tmp_path, copied):
    with pytest.raises(UsageError, match=re.escape("give the field a generator in [program.CHILD")):
        effective(
            tmp_path,
            f"""
[program.CHILD.from]
enrollments = {{ of = "PREGNANCY", as = "pregnancy" }}
attributes = {{ FAMILY_NAME = {copied} }}
""",
        )


def test_an_option_code_the_option_set_does_not_hold_fails(tmp_path):
    with pytest.raises(UsageError, match="are not in its option set"):
        effective(
            tmp_path,
            "[program.PREGNANCY]\n[program.PREGNANCY.values]\n"
            '"BLOOD_GROUP" = { weights = { NOT_A_CODE = 1 } }',
        )


def test_an_option_set_whose_codes_do_not_fit_the_value_type_gives_no_default(tmp_path):
    eff, metadata = compile_stage.compile_build(Build(spec=write_spec(tmp_path, ""), root=tmp_path))
    field = metadata.data_element("Confirmed01")
    assert field.option_set is not None
    codes = {(field.value_type, o.code) for o in metadata.options(field.option_set)}
    gen, says = spec.default_generator(field, metadata, eff["build"], spec.valid_values(codes))
    assert gen is None
    assert "TRUE_ONLY" in says


def test_option_codes_are_valid_as_the_pre_load_checks_judge_them():
    pairs = {
        ("INTEGER_POSITIVE", "0"),
        ("INTEGER_POSITIVE", "1"),
        ("INTEGER_POSITIVE", "-5"),
        ("INTEGER_POSITIVE", "007"),
        ("PERCENTAGE", "150"),
        ("PERCENTAGE", "50"),
        ("TIME", "25:00"),
        ("TEXT", "LIVE_BIRTH"),
    }
    assert spec.valid_values(pairs) == {
        ("INTEGER_POSITIVE", "1"),
        ("PERCENTAGE", "50"),
        ("TEXT", "LIVE_BIRTH"),
    }


@pytest.mark.parametrize(
    ("body", "where"),
    [
        (
            '[program.PREGNANCY]\ntracked_entities = { per_orgunit = 10, by = "facility.nope" }',
            "program.PREGNANCY.tracked_entities",
        ),
        # The stage's compulsory SYSTOLIC and DIASTOLIC come before NOTES in one statement.
        (
            '[program.PREGNANCY.values]\nNOTES = { sql = "facility.nope" }',
            "program.PREGNANCY.values.NOTES",
        ),
    ],
)
def test_an_expression_that_does_not_bind_fails_compile_with_its_place(tmp_path, body, where):
    with pytest.raises(UsageError, match=re.escape(f"does not bind ({where})")):
        compile_stage.run(Build(spec=write_spec(tmp_path, body), root=tmp_path))


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            '[program.PREGNANCY]\ntracked_entities = { per_orgunit = 10, by = "facilty.size" }',
            "tracked_entities.by: names facilty.size: no scope facilty; facility?",
        ),
        (
            '[program.PREGNANCY]\ntracked_entities = { per_orgunit = 10, by = "enrollment.seq" }',
            "tracked_entities.by: names enrollment.seq, but sees only district, facility, "
            "orgunit, perf_land, region here",
        ),
        (
            '[trait.enrollment]\nlate = { sql = "event.date - enrollment.date" }',
            "trait.enrollment.late: names event.date, but sees only district, enrollment, ",
        ),
        (
            '[program.PREGNANCY.values]\nHB = { constant = 12, if = "enrollment.number > 0 '
            'and evnt.number > 0" }',
            "values.HB.if: names evnt.number: no scope evnt; event?",
        ),
    ],
)
def test_an_expression_naming_a_scope_it_does_not_see_fails(tmp_path, body, error):
    eff = effective(tmp_path, body)
    with pytest.raises(UsageError, match=re.escape(error)):
        generate.statements(eff)


def test_an_expression_reads_no_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    read = f"(select content from read_text('{secret}'))"
    eff = effective(tmp_path, f'[program.PREGNANCY.values]\nNOTES = {{ sql = "{read}" }}')
    with pytest.raises(UsageError, match="file system operations are disabled"):
        generate.check_binds(eff, generate.statements(eff))


def test_an_expression_may_name_a_lambdas_fields(tmp_path):
    eff = effective(
        tmp_path,
        "[program.PREGNANCY.values]\nHB = { sql = \"list_sum(list_transform([{'lam': 1}], "
        'lambda p: p.lam))" }',
    )
    generate.check_binds(eff, generate.statements(eff))


def test_unvalidated_names_fields_of_its_program(tmp_path):
    eff = effective(
        tmp_path,
        '[program.PREGNANCY]\nunvalidated = ["Age (years)", "Systolic001"]',
    )
    assert eff["unvalidated"]["PREGNANCY"] == ["AgeYears001", "Systolic001"]
    assert "unvalidated" not in program(eff, "PREGNANCY")


def test_an_unvalidated_field_the_program_lacks_fails(tmp_path):
    with pytest.raises(UsageError, match="unvalidated"):
        effective(tmp_path, '[program.PREGNANCY]\nunvalidated = ["NOPE"]')


def test_metadata_is_read_next_to_the_spec_wherever_dbgen_runs(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    (spec_dir / "metadata").mkdir(parents=True)
    (spec_dir / "metadata" / "m.json").write_bytes(open(METADATA[0], "rb").read())
    path = write_spec(spec_dir)
    path.write_text(
        path.read_text().replace(f"metadata = {METADATA!r}", 'metadata = ["metadata/m.json"]')
    )
    # A file of the same name where dbgen runs, which the spec does not mean.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "metadata").mkdir(parents=True)
    (elsewhere / "metadata" / "m.json").write_text('{"programs": []}')
    found = []
    for directory in (elsewhere, spec_dir, tmp_path):
        monkeypatch.chdir(directory)
        eff, _ = compile_stage.compile_build(Build(spec=path, root=tmp_path))
        [entry] = eff["build"]["metadata"]
        assert entry["file"] == "metadata/m.json"
        found.append(provenance.digests(eff)["metadata"])
    assert len(set(found)) == 1


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("[nope]\nx = 1", "unknown key nope"),
        ("[program.PREGNANCY]\nfill = 0", "unknown key fill"),
        ("[records.updated]\nshare = 7", "records.updated.share: must be a share from 0 to 1"),
        ("[program.PREGNANCY.enrollments]\nstatus = { ACTIVE = true }", "shares must be numbers"),
        ("[program.PREGNANCY]\ntracked_entities = true", "must be a table"),
        (
            "[program.PREGNANCY.attributes]\nGIVEN_NAME = { unique = false }",
            "unique: takes only true",
        ),
        (
            "[program.PREGNANCY.attributes]\nGIVEN_NAME = { words = { n = 10 }, max = 3 }",
            "program.PREGNANCY.attributes.GIVEN_NAME: words takes no max; only a distribution "
            "takes min, max, round",
        ),
        (
            "[program.PREGNANCY.attributes]\nPHONE = { phone = true, round = 0 }",
            "attributes.PHONE: phone takes no round",
        ),
        ('[trait.district]\n"people-count" = 5', "trait.district.people-count: a trait's name"),
        ("[trait.district]\nname = 5", "every level has a name of its own"),
        ("[trait.enrollment]\ndate = 5", "every enrollment has a date of its own"),
        (
            '[[orgunits.level]]\nname = "Post"\ntypes = [{ name = "Small", count = 1, size = 2 }]',
            "orgunits.level[3].types Small: unknown key size; known: clerks, count, name",
        ),
        (
            '[trait.district]\nbig = { largest = "weight", untill_share = 0.1 }',
            "trait.district.big: unknown key untill_share; largest takes until_share",
        ),
        (
            '[trait.district]\nbig = { largest = "weight", until_share = 0.1, round = 2 }',
            "trait.district.big: unknown key round",
        ),
        ('[trait.district]\nbig = { largest = "weight" }', "largest needs until_share"),
        (
            '[trait.district]\nbig = { largest = "weight", until_share = 7 }',
            "trait.district.big.until_share: must be a share from 0 to 1",
        ),
        (
            '[trait.district]\nbig = { largest = "weight", until_share = "x" }',
            "trait.district.big.until_share: must be a share from 0 to 1",
        ),
        ("[trait.tracked_entity]\nseq = 5", "every tracked entity has a seq of its own"),
        ("[program.PREGNANCY.trait.enrollment]\nNumber = 5", "a trait's name holds lowercase"),
        (
            '[program.PREGNANCY.trait.enrollment]\nlate = "event.date - enrollment.date"',
            "program.PREGNANCY.trait.enrollment.late: a trait is a number, and a string is text; "
            'give an expression as { sql = "event.date - enrollment.date" }',
        ),
        (
            '[trait.district]\nbig = { constant = "district.weight * 5" }',
            "trait.district.big: a trait is a number, and a string is text; give an expression as "
            '{ sql = "district.weight * 5" }',
        ),
        (
            '[program.PREGNANCY.values]\nHB = "enrollment.risk + 1"',
            "values.HB: 'enrollment.risk + 1' is not a value of type NUMBER; a string is text, an "
            'expression { sql = "enrollment.risk + 1" }',
        ),
        (
            '[program.PREGNANCY.values]\nOUTCOME = "LIVE_BRTH"',
            "values.OUTCOME: 'LIVE_BRTH' is not in its option set, which holds ['LIVE_BIRTH', ",
        ),
    ],
)
def test_a_value_the_spec_does_not_mean_fails(tmp_path, body, error):
    with pytest.raises(UsageError, match=re.escape(error)):
        effective(tmp_path, body)


def test_a_string_of_a_value_is_written_as_it_is_if_its_field_takes_it(tmp_path):
    eff = effective(tmp_path, '[program.PREGNANCY.values]\nHB = "12.5"\nLMP = "2024-02-03"')
    values = {v["code"]: v for s in program(eff, "PREGNANCY")["stages"] for v in s["values"]}
    assert (values["HB"]["gen"], values["LMP"]["gen"]) == ("12.5", "2024-02-03")
    with pytest.raises(UsageError, match=r"'2024-02-30' is not a value of type DATE$"):
        effective(tmp_path, '[program.PREGNANCY.values]\nLMP = "2024-02-30"')


def test_the_generators_that_are_no_distribution_are_those_generate_draws_apart():
    assert set(spec.SPECIAL) == set(generate.SPECIAL)


@pytest.mark.parametrize("parameter", ["inf", "nan", "true"])
def test_a_parameter_that_is_no_finite_number_fails(tmp_path, parameter):
    eff = effective(
        tmp_path, f"[program.PREGNANCY.enrollments]\ncount = {{ poisson = {parameter} }}"
    )
    with pytest.raises(UsageError, match=r"^program\.PREGNANCY\.enrollments\.poisson: .* not a"):
        generate.statements(eff)


@pytest.mark.parametrize("name", ["PregProgram", "Pregnancy"])
def test_an_error_names_a_program_by_its_key_however_the_spec_names_it(tmp_path, name):
    eff = effective(tmp_path, f"[program.{name}.values]\nNOTES = {{ sql = \"u('X')\" }}")
    with pytest.raises(UsageError, match=r"^program\.PREGNANCY\.values\.NOTES: u\('X'\)"):
        generate.statements(eff)


LINKED = 'enrollments = { of = "PREGNANCY", as = "pregnancy" }'


@pytest.mark.parametrize(
    ("traits", "error"),
    [
        ("[program.PREGNANCY.trait.enrollment]\nend = 5", "trait end would be pregnancy.end"),
        (
            "[program.PREGNANCY.trait.enrollment]\nrisk = 5\n"
            "[program.PREGNANCY.trait.tracked_entity]\nrisk = 5",
            "an enrollment trait risk, both pregnancy.risk",
        ),
    ],
)
def test_a_source_trait_names_one_column(tmp_path, traits, error):
    with pytest.raises(UsageError, match=re.escape(error)):
        effective(tmp_path, f"{traits}\n[program.CHILD.from]\n{LINKED}")


def test_own_columns_are_those_generate_gives_each_scope(tmp_path):
    eff = effective(
        tmp_path,
        '[program.CHILD.from]\nenrollments = { of = "PREGNANCY", as = "pregnancy" }',
    )
    con = duckdb.connect()
    generate.prepare(con, eff)
    generate.empty_inputs(con)
    for statement in generate.statements(eff):
        con.execute(statement)
    columns = {
        c
        for (table,) in con.execute("select table_name from duckdb_tables()").fetchall()
        for c in (d[0] for d in con.execute(f'select * from "{table}" limit 0').description)
    }

    def own(scope: str) -> set[str]:
        return {c.split(".", 1)[1] for c in columns if c.startswith(f"{scope}.")}

    for level in ("region", "district", "facility"):
        assert own(level) == set(spec.OWN_COLUMNS["level"]), level
    assert own("tracked_entity") == set(spec.OWN_COLUMNS["tracked_entity"])
    assert own("enrollment") == set(spec.OWN_COLUMNS["enrollment"])
    assert own("pregnancy") == set(spec.OWN_COLUMNS["source"])


def test_programs_whose_tables_would_share_a_name_fail(tmp_path):
    metadata = json.loads(open(METADATA[0]).read())
    child = next(p for p in metadata["programs"] if p.get("code") == "CHILD")
    child["code"] = "Pregnancy"
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata))
    spec_path = write_spec(tmp_path)
    spec_path.write_text(spec_path.read_text().replace(repr(METADATA), repr([str(path)])))
    with pytest.raises(UsageError, match="PREGNANCY and Pregnancy have codes that differ only"):
        compile_stage.compile_build(Build(spec=spec_path, root=tmp_path))


def test_metadata_with_an_event_program_fails(tmp_path):
    metadata = json.loads(open(METADATA[0]).read())
    metadata["programs"].append(
        {
            "id": "EventProgra",
            "code": "EVENTS",
            "name": "Events",
            "programType": "WITHOUT_REGISTRATION",
        }
    )
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata))
    spec_path = write_spec(tmp_path)
    spec_path.write_text(spec_path.read_text().replace(repr(METADATA), repr([str(path)])))
    with pytest.raises(UsageError, match=re.escape("event programs EVENTS (Events)")):
        compile_stage.compile_build(Build(spec=spec_path, root=tmp_path))


@pytest.mark.parametrize(
    ("pattern", "segment"),
    [
        ('ORG_UNIT_CODE(...) + "/" + RANDOM(###)', "ORG_UNIT_CODE(...)"),
        ("RANDOM(XXX######)", "RANDOM(XXX######)"),
        ('"ID-" + CURRENT_DATE(yyyy)', "CURRENT_DATE(yyyy)"),
    ],
)
def test_a_pattern_of_a_segment_dbgen_does_not_draw_fails(tmp_path, pattern, segment):
    eff = effective(
        tmp_path, f"[program.PREGNANCY.attributes]\nUNIQUE_ID = {{ pattern = '{pattern}' }}"
    )
    with pytest.raises(
        UsageError, match=re.escape(f"attributes.UNIQUE_ID: pattern {pattern!r}")
    ) as e:
        generate.statements(eff)
    assert f"not {segment};" in str(e.value)


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda t: "trait = 5\n" + t, "trait: must be a table"),
        (lambda t: t.replace("seed = 7", "seed = 7\nyears = 2"), "unknown key years"),
        (lambda t: t.replace("start = 2023-01-01", "start = 2023-01-01T08:00:00"), "date and time"),
        (lambda t: t + "\n[program.PREGNANCY.values]\nNOTES = { words = { count = 0 } }", "words"),
        (lambda t: t + '\n[program.PREGNANCY.values]\nNOTES = { words = { n = "many" } }', "words"),
        (lambda t: t + "\n[program.PREGNANCY.values]\nNOTES = { words = { q = -1 } }", "words: q"),
        (lambda t: t + "\n[program.PREGNANCY.values]\nNOTES = { words = { q = nan } }", "words: q"),
        (lambda t: t + "\n[program.PREGNANCY.values]\nNOTES = { words = { s = inf } }", "words: s"),
        (
            lambda t: t + "\n[program.PREGNANCY.values]\nNOTES = { words = { offset = 25 } }",
            "words: unknown key offset",
        ),
        (lambda t: t.replace("per_parent = 2", 'per_parent = 2\nclerks = "x"'), "clerks: must be"),
        (
            lambda t: t.split("[[orgunits.level]]")[0] + "level = [1]",
            "must be a table",
        ),
    ],
)
def test_a_spec_of_the_wrong_shape_fails_with_a_message(tmp_path, edit, error):
    path = write_spec(tmp_path)
    path.write_text(edit(path.read_text()))
    with pytest.raises(UsageError, match=re.escape(error)):
        eff, _ = compile_stage.compile_build(Build(spec=path, root=tmp_path))
        generate.statements(eff)


def test_a_window_from_a_leap_day_ends_on_feb_28(tmp_path):
    path = write_spec(tmp_path)
    path.write_text(
        path.read_text().replace("start = 2023-01-01\nend = 2025-01-01", "start = 2024-02-29")
    )
    eff, _ = compile_stage.compile_build(Build(spec=path, root=tmp_path))
    assert (eff["build"]["start"], eff["build"]["end"]) == ("2024-02-29", "2029-02-28")


def test_a_stage_assigns_events_only_if_its_metadata_enables_user_assignment(tmp_path):
    eff = effective(tmp_path, "[program.PREGNANCY.stage.Check]\nassigned = 0.2")
    stages = {s["name"]: s["assigned"] for s in program(eff, "PREGNANCY")["stages"]}
    assert stages == {"Profile": 0.0, "Check": 0.2, "Lab": 0.0, "Close": 0.0}
    with pytest.raises(UsageError, match="stage.Lab.assigned: the stage does not enable user"):
        effective(tmp_path, "[program.PREGNANCY.stage.Lab]\nassigned = 0.2")


def test_records_take_no_assigned(tmp_path):
    with pytest.raises(UsageError, match=re.escape("records.assigned: set it on the stage")):
        effective(tmp_path, "[records]\nassigned = 0.05")


def test_enrollments_take_the_share_of_them_followed_up(tmp_path):
    eff = effective(tmp_path, "[program.PREGNANCY.enrollments]\nfollowup = 0.03")
    assert {p["key"]: p["followup"] for p in eff["programs"]} == {"PREGNANCY": 0.03, "CHILD": 0.0}
    with pytest.raises(UsageError, match=re.escape("enrollments.followup: must be a share")):
        effective(tmp_path, "[program.PREGNANCY.enrollments]\nfollowup = 3")


def test_records_take_no_followup(tmp_path):
    with pytest.raises(UsageError, match=re.escape("records.followup: set it on the enrollments")):
        effective(tmp_path, "[records]\nfollowup = 0.03")
