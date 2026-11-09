"""`dbgen validate`: every record of an area through DHIS2's tracker importer, to check that the
spec satisfies the program rules.

load writes past the importer, so the rules that would refuse or change a value do not run on the
records a build loads: satisfying them is the spec's job. validate checks it, on a small area
before a large build:

1. It runs the stages of `dbgen sample` and bootstrap that are not current (provenance.fresh):
   compile, orgunits, users, generate and bootstrap, over the area of --orgunit.
2. It starts DHIS2 on the build database bootstrap made, which must hold no tracker records yet,
   and submits every tracked entity that is not deleted, with its attributes, enrollments, events
   and relationships, to the tracker importer with importMode=VALIDATE (importer.py): DHIS2's own
   rule engine runs every program rule on every record, and nothing is written. A tracked entity
   and those it is related to go in one payload, so a relationship finds both its ends. The values
   of the spec's unvalidated fields are left out: a rule assigns them from what the importer cannot
   reproduce for a record of the past, such as today's date.
3. It writes output/stage/<build>/validate/report.json, and prints the errors and warnings by
   code, program rule and field, with how many records each failed: the spec field to change is
   plain. Any error fails.
"""

import json
import sys
from collections import defaultdict

import duckdb

from dbgen import compile as compile_stage
from dbgen import provenance, timing
from dbgen.config import Build, shown
from dbgen.dhis2 import client, importer
from dbgen.errors import UsageError

# Tracked entities per payload: few enough for the importer to answer in seconds.
CHUNK = 50
STAGES = ("compile", "orgunits", "users", "generate", "bootstrap")


def run(build: Build) -> dict:
    from dbgen.cli import run_stage

    for stage in STAGES:
        current, _ = compile_stage.compile_build(build)
        # The build database is one for every build of a root: bootstrap is current only while it
        # holds this build's bootstrap.
        if provenance.fresh(build, stage, current) and (
            stage != "bootstrap"
            or _holds(build.project, provenance.load(build)["bootstrap"]["run"])
        ):
            # fresh compares the spec and the stages read, not the code they ran from.
            change = provenance.code_change(provenance.load(build)[stage], provenance.code())
            if change:
                print(
                    f"validate: {stage} is up to date with the spec, but {change}. Rerun it if "
                    f"the code change matters: dbgen stage {stage} {shown(build.spec)}",
                    file=sys.stderr,
                )
            else:
                print(f"validate: {stage} is up to date", file=sys.stderr)
            continue
        run_stage(build, stage, reset=stage == "bootstrap")
    effective, parents = compile_stage.inputs(build, "validate")
    with timing.timed(build.dir, "validate"):
        skip = importer.unvalidated(effective)
        payloads = [importer.without(p, skip) for p in _payloads(build, effective)]
        with client.running(build.project):
            with client.connect(build.project, autocommit=True) as pg, pg.cursor() as cur:
                provenance.check_database(cur, parents["bootstrap"]["run"])
                cur.execute("select exists (select from trackedentity)")
                (loaded,) = cur.fetchone() or (False,)
                if loaded:
                    raise UsageError(
                        "the build database holds the tracker records of a load, whose UIDs and "
                        "unique values the records to validate repeat; empty it with dbgen stage "
                        f"bootstrap {shown(build.spec)} --reset, then run dbgen validate again"
                    )
                known = importer.names(cur)
            errors, warnings = importer.Tally(), importer.Tally()
            with client.api(build.project) as api:
                for i, payload in enumerate(payloads, 1):
                    importer.validate(api, payload, errors, warnings, known)
                    if i % 20 == 0 or i == len(payloads):
                        print(
                            f"validate: {i} of {len(payloads)} payloads, {errors.total()} errors",
                            file=sys.stderr,
                        )
    counts = {
        "trackedEntities": sum(len(p["trackedEntities"]) for p in payloads),
        "enrollments": sum(len(t["enrollments"]) for p in payloads for t in p["trackedEntities"]),
        "events": sum(
            len(e["events"])
            for p in payloads
            for t in p["trackedEntities"]
            for e in t["enrollments"]
        ),
        "relationships": sum(len(p["relationships"]) for p in payloads),
    }
    result = {"validated": counts, "errors": errors.rows(known), "warnings": warnings.rows(known)}
    output = build.make_stage_dir("validate")
    (output / "report.json").write_text(json.dumps(result, indent=1) + "\n")
    text = importer.summary(f"validate {build.name} {counts}", errors, warnings, known, limit=40)
    print(text, file=sys.stderr)
    if errors.total():
        raise UsageError(
            f"DHIS2's importer refuses the spec's records with {errors.total()} errors: change "
            f"the fields of the spec that the rules above name. {shown(output / 'report.json')} "
            "lists every error"
        )
    provenance.record(build, "validate", parents, effective)
    return result


def _holds(project: str, run: str) -> bool:
    """Whether the build database holds the bootstrap of run."""
    with client.running(project, ("db",), timeout=300):
        with client.connect(project, autocommit=True) as pg, pg.cursor() as cur:
            try:
                provenance.check_database(cur, run)
            except UsageError:
                return False
    return True


def _payloads(build: Build, effective: dict):
    """The tracker payloads of the area: the tracked entities that are not deleted, in groups of
    those related to each other, at most CHUNK each but for a group larger than that. generate's
    tables reference the metadata by UID, as the importer does."""
    del effective
    con = duckdb.connect()
    for table in (
        "trackedentity",
        "trackedentityattributevalue",
        "enrollment",
        "event",
        "relationship",
        "relationshipitem",
    ):
        con.execute(
            f"create table {table} as select * from read_parquet(?)",
            [str(build.stage_dir("generate") / f"{table}.parquet")],
        )
    tes = con.execute(
        """
        select trackedentityid, uid, trackedentitytype_uid, organisationunit_uid
        from trackedentity
        where not deleted
        order by trackedentityid
        """
    ).fetchall()
    # Every attribute goes on the tracked entity, which the importer takes for its type's and
    # its programs'.
    attributes: dict[int, list[dict]] = defaultdict(list)
    for te, tea, value in con.execute(
        "select trackedentityid, trackedentityattribute_uid, value "
        "from trackedentityattributevalue order by 1, 2"
    ).fetchall():
        attributes[te].append({"attribute": tea, "value": value})
    events: dict[int, list[dict]] = defaultdict(list)
    for en, uid, stage, orgunit, status, occurred, scheduled, completed, aoc, values in con.execute(
        """
        select enrollmentid, uid, programstage_uid, organisationunit_uid, status,
               strftime(occurreddate, '%Y-%m-%d'), strftime(scheduleddate, '%Y-%m-%d'),
               strftime(completeddate, '%Y-%m-%dT%H:%M:%S'), attributeoptioncombo_uid,
               datavalues
        from event
        where not deleted
        order by eventid
        """
    ).fetchall():
        event = {
            "event": uid,
            "programStage": stage,
            "orgUnit": orgunit,
            "status": status,
            "scheduledAt": scheduled,
            "dataValues": [{"dataElement": v["k"], "value": v["v"]} for v in values],
        }
        if occurred:
            event["occurredAt"] = occurred
        if completed:
            event["completedAt"] = completed
        if aoc:
            event["attributeOptionCombo"] = aoc
        events[en].append(event)
    enrollments: dict[int, list[dict]] = defaultdict(list)
    for (
        en,
        te,
        uid,
        program,
        orgunit,
        status,
        enrolled,
        occurred,
        completed,
        followup,
    ) in con.execute(
        """
            select enrollmentid, trackedentityid, uid, program_uid, organisationunit_uid, status,
                   strftime(enrollmentdate, '%Y-%m-%d'), strftime(occurreddate, '%Y-%m-%d'),
                   strftime(completeddate, '%Y-%m-%dT%H:%M:%S'), followup
            from enrollment
            where not deleted
            order by enrollmentid
            """
    ).fetchall():
        # Each event names its program, enrollment and tracked entity, as DHIS2's export writes
        # them: without its program, the importer refuses an attribute a rule assigns to the
        # enrollment as not of the program (E1019).
        for event in events.get(en, []):
            event |= {"program": program, "enrollment": uid}
        enrollment = {
            "enrollment": uid,
            "program": program,
            "orgUnit": orgunit,
            "status": status,
            "enrolledAt": enrolled,
            "occurredAt": occurred,
            "followUp": bool(followup),
            "events": events.get(en, []),
        }
        if completed:
            enrollment["completedAt"] = completed
        enrollments[te].append(enrollment)
    for te, uid, _, _ in tes:
        for enrollment in enrollments.get(te, []):
            for event in enrollment["events"]:
                event["trackedEntity"] = uid
    tracked = {
        te: {
            "trackedEntity": uid,
            "trackedEntityType": tet,
            "orgUnit": orgunit,
            "attributes": attributes[te],
            "enrollments": enrollments.get(te, []),
        }
        for te, uid, tet, orgunit in tes
    }
    rels = con.execute(
        """
        select r.uid, r.relationshiptype_uid, f.trackedentityid, t.trackedentityid
        from relationship r
        join relationshipitem f on f.relationshipitemid = r.from_relationshipitemid
        join relationshipitem t on t.relationshipitemid = r.to_relationshipitemid
        where not r.deleted
        """
    ).fetchall()
    # Groups of related tracked entities, by union-find, so a relationship finds both its ends.
    parent = {te: te for te in tracked}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, _, a, b in rels:
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for te in tracked:
        groups[root(te)].append(te)
    of_group: dict[int, list[dict]] = defaultdict(list)
    for uid, rt, a, b in rels:
        if a in tracked and b in tracked:
            of_group[root(a)].append(
                {
                    "relationship": uid,
                    "relationshipType": rt,
                    "from": {"trackedEntity": {"trackedEntity": tracked[a]["trackedEntity"]}},
                    "to": {"trackedEntity": {"trackedEntity": tracked[b]["trackedEntity"]}},
                }
            )
    payload: dict = {"trackedEntities": [], "relationships": []}
    for key, members in groups.items():
        payload["trackedEntities"] += [tracked[te] for te in members]
        payload["relationships"] += of_group.get(key, [])
        if len(payload["trackedEntities"]) >= CHUNK:
            yield payload
            payload = {"trackedEntities": [], "relationships": []}
    if payload["trackedEntities"]:
        yield payload
