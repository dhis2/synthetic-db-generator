"""compile, orgunits, users and generate on a tiny spec: what `dbgen sample` runs, without
Docker."""

import json
import re
import shutil

import duckdb

from dbgen import compile as compile_stage
from dbgen import generate, orgunits, users
from dbgen.config import Build
from tests.conftest import write_spec


def sample(build: Build) -> dict:
    compile_stage.run(build)
    orgunits.run(build)
    users.run(build)
    generate.run(build)
    return json.loads((build.stage_dir("generate") / "checksums.json").read_text())


def test_two_samples_give_the_same_rows(tiny):
    first = sample(tiny)
    shutil.rmtree(tiny.dir)
    assert sample(tiny) == first
    assert first["event"]["rows"] > 0


def test_another_seed_gives_other_rows(tmp_path):
    builds = []
    for seed in (7, 8):
        directory = tmp_path / str(seed)
        directory.mkdir()
        spec = write_spec(directory)
        spec.write_text(spec.read_text().replace("seed = 7", f"seed = {seed}"))
        builds.append(Build(spec=spec, root=directory))
    a, b = (sample(build) for build in builds)
    assert a["event"]["row_digest"] != b["event"]["row_digest"]


def test_the_records_fit_the_window_and_the_statuses(tmp_path):
    (tmp_path / "s").mkdir()
    build = Build(
        spec=write_spec(
            tmp_path / "s",
            """
[program.PREGNANCY]
tracked_entities = 20
enrollments = { count = { poisson = 0.5, plus = 1 }, status = { COMPLETED = 1 } }

[program.PREGNANCY.visits]
stages = ["Check", "Lab"]
count = { poisson = 3, min = 1 }
spacing = { uniform = [28, 42] }
status = { COMPLETED = 0.9, SKIPPED = 0.1 }
""",
        ),
        root=tmp_path / "s",
    )
    sample(build)
    out = build.stage_dir("generate")
    con = duckdb.connect()
    [(outside,)] = con.execute(
        f"select count(*) from '{out}/event.parquet' "
        "where occurreddate < date '2023-01-01' or occurreddate >= date '2025-01-01'"
    ).fetchall()
    assert outside == 0
    # The events of one visit share their day and status.
    [(split,)] = con.execute(
        f"""
        select count(*) from (
            select enrollmentid, scheduleddate, count(distinct status) as statuses
            from '{out}/event.parquet'
            where programstage_uid in ('PregCheck01', 'PregLab0001')
            group by all having statuses > 1)
        """
    ).fetchall()
    assert split == 0
    statuses = dict(
        con.execute(
            f"select status, count(*) from '{out}/enrollment.parquet' "
            "where program_uid = 'PregProgram' group by all"
        ).fetchall()
    )
    assert set(statuses) <= {"COMPLETED", "ACTIVE"}
    [(values,)] = con.execute(
        f"select count(*) from '{out}/event.parquet' "
        "where status = 'SKIPPED' and len(datavalues) > 0"
    ).fetchall()
    assert values == 0


def test_a_count_spreads_evenly_over_the_parents(tmp_path):
    (tmp_path / "t").mkdir()
    spec = (
        write_spec(tmp_path / "t")
        .read_text()
        .replace('name = "District"\nper_parent = 1', 'name = "District"\ncount = 5')
    )
    path = tmp_path / "t" / "tiny.toml"
    path.write_text(spec)
    build = Build(spec=path, root=tmp_path / "t")
    compile_stage.run(build)
    orgunits.run(build)
    rows = (
        duckdb.connect()
        .execute(
            f"select parent_code, count(*) from '{build.stage_dir('orgunits')}/orgunits.parquet' "
            "where level = 3 group by all order by all"
        )
        .fetchall()
    )
    assert rows == [("QP01", 3), ("QP02", 2)]


def test_facilities_of_a_type_level_carry_its_weight_and_clerks(tmp_path):
    (tmp_path / "t").mkdir()
    spec = (
        write_spec(tmp_path / "t")
        .read_text()
        .replace(
            '[[orgunits.level]]\nname = "Facility"\nper_parent = 2',
            """[[orgunits.level]]
name = "Facility"
per = "District"
types = [
  { name = "Health Post", count = 3, weight = 1, clerks = 1 },
  { name = "Hospital", count = 1, weight = 6, clerks = 3 },
]""",
        )
    )
    path = tmp_path / "t" / "tiny.toml"
    path.write_text(spec)
    build = Build(spec=path, root=tmp_path / "t")
    compile_stage.run(build)
    orgunits.run(build)
    users.run(build)
    con = duckdb.connect()
    rows = con.execute(
        f"select type, count(*), min(clerks), max((traits::json->>'weight')::double) "
        f"from '{build.stage_dir('orgunits')}/orgunits.parquet' where level = 4 group by all "
        "order by all"
    ).fetchall()
    assert rows == [("Health Post", 6, 1, 1.0), ("Hospital", 2, 3, 6.0)]
    [(clerks,)] = con.execute(
        f"select count(*) from '{build.stage_dir('users')}/users.parquet' where kind = 'clerk'"
    ).fetchall()
    assert clerks == 6 * 1 + 2 * 3


def test_an_area_draws_the_records_its_org_units_draw_in_the_whole(tmp_path):
    """A total shared among a district's facilities gives each the same share whether the build
    covers the district or all of the hierarchy."""
    body = """
[program.PREGNANCY]
tracked_entities = { total = 60, among = "District", by = "facility.weight" }
"""
    counts = []
    for codes in ((), ("QP0101",)):
        directory = tmp_path / ("all" if not codes else "area")
        directory.mkdir()
        build = Build(spec=write_spec(directory, body), codes=codes, root=directory)
        sample(build)
        counts.append(
            dict(
                duckdb.connect()
                .execute(
                    f"select organisationunit_uid, count(*) from "
                    f"'{build.stage_dir('generate')}/enrollment.parquet' "
                    "where program_uid = 'PregProgram' group by all"
                )
                .fetchall()
            )
        )
    whole, area = counts
    assert area and all(whole[ou] == n for ou, n in area.items())
    assert sum(area.values()) < sum(whole.values())


def test_a_value_may_name_another_value_of_its_event(tmp_path):
    body = """
[program.PREGNANCY]
tracked_entities = 5

[program.PREGNANCY.values]
Systolic001 = { sql = "120" }
Diastolic01 = { sql = "value.Systolic001::double - 40" }
"""
    build = Build(spec=write_spec(tmp_path, body), root=tmp_path)
    sample(build)
    pairs = (
        duckdb.connect()
        .execute(
            f"""
        select list_filter(datavalues, lambda v: v.k = 'Systolic001')[1].v,
               list_filter(datavalues, lambda v: v.k = 'Diastolic01')[1].v
        from '{build.stage_dir("generate")}/event.parquet'
        where list_contains(list_transform(datavalues, lambda v: v.k), 'Systolic001')
        """
        )
        .fetchall()
    )
    assert pairs and set(pairs) == {("120", "80")}


def test_a_value_is_written_only_if_its_condition_holds(tmp_path):
    body = """
[program.PREGNANCY]
tracked_entities = 10

[program.PREGNANCY.values]
Systolic001 = { uniform = [80, 140], round = 0 }
Notes000001 = { constant = "high", if = "value.Systolic001::double > 100" }
"""
    build = Build(spec=write_spec(tmp_path, body), root=tmp_path)
    sample(build)
    rows = (
        duckdb.connect()
        .execute(
            f"""
        select list_filter(datavalues, lambda v: v.k = 'Systolic001')[1].v::int,
               list_filter(datavalues, lambda v: v.k = 'Notes000001')[1].v
        from '{build.stage_dir("generate")}/event.parquet'
        where list_contains(list_transform(datavalues, lambda v: v.k), 'Systolic001')
        """
        )
        .fetchall()
    )
    assert {notes for systolic, notes in rows if systolic > 100} == {"high"}
    assert {notes for systolic, notes in rows if systolic <= 100} == {None}


def test_a_pattern_draws_its_literals_and_digits(tmp_path):
    body = """
[program.PREGNANCY]
tracked_entities = 10

[program.PREGNANCY.attributes]
UNIQUE_ID = { pattern = '"ID-" + SEQUENTIAL(###) + "-" + RANDOM(##)' }
"""
    build = Build(spec=write_spec(tmp_path, body), root=tmp_path)
    sample(build)
    values = [
        value
        for (value,) in duckdb.connect()
        .execute(
            f"""
        select value from '{build.stage_dir("generate")}/trackedentityattributevalue.parquet'
        where trackedentityattribute_uid = 'UniqueId001'
        """
        )
        .fetchall()
    ]
    assert values
    assert all(re.fullmatch(r"ID-\d{3}-\d{2}", v) for v in values), values[:5]
    assert len(set(values)) == len(values)


def test_a_linked_program_draws_its_tracked_entities_from_the_sources_enrollments(tmp_path):
    body = """
[program.PREGNANCY]
tracked_entities = 30

[program.PREGNANCY.attributes]
FAMILY_NAME = { words = { n = 1000 } }

[program.CHILD]

[program.CHILD.from]
enrollments = { of = "PREGNANCY", as = "mother" }
where = "mother.number = 0"
relationship = "Mother and child"
at = "mother.end + 3"
attributes = { FAMILY_NAME = "mother.FAMILY_NAME" }
"""
    build = Build(spec=write_spec(tmp_path, body), root=tmp_path)
    counts = sample(build)
    con = duckdb.connect()
    con.execute(f"set file_search_path = '{build.stage_dir('generate')}'")
    [(children, linked, wrong)] = con.execute(
        """
        with link as (
            select f.trackedentityid as mother, t.trackedentityid as child
            from 'relationship.parquet' r
            join 'relationshipitem.parquet' f on f.relationshipitemid = r.from_relationshipitemid
            join 'relationshipitem.parquet' t on t.relationshipitemid = r.to_relationshipitemid
        ),
        family as (
            select trackedentityid, value from 'trackedentityattributevalue.parquet'
            where trackedentityattribute_uid = 'FamilyName1'
        ),
        mother_end as (
            select e.trackedentityid, e.organisationunit_uid,
                   max(coalesce(ev.occurreddate, ev.scheduleddate)) as last_day
            from 'enrollment.parquet' e
            join 'event.parquet' ev using (enrollmentid)
            where e.program_uid = 'PregProgram'
              and e.enrollmentid = (select min(enrollmentid) from 'enrollment.parquet' x
                                    where x.trackedentityid = e.trackedentityid)
            group by all
        )
        select (select count(*) from 'enrollment.parquet' where program_uid = 'ChildProgra'),
               count(*),
               count(*) filter (where fm.value <> fc.value
                                   or c.organisationunit_uid <> m.organisationunit_uid
                                   or c.enrollmentdate::date <> m.last_day::date + 3)
        from link
        join family fm on fm.trackedentityid = link.mother
        join family fc on fc.trackedentityid = link.child
        join 'enrollment.parquet' c on c.trackedentityid = link.child
        join mother_end m on m.trackedentityid = link.mother
        """
    ).fetchall()
    # Every child is linked, as the program draws none of its own, and born of its mother's first
    # pregnancy, 3 days after its last event, at its org unit, with her family name.
    assert counts["relationship"]["rows"] == children == linked > 0
    assert wrong == 0
