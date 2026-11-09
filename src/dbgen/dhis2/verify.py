"""`dbgen stage verify`: checks on the loaded database, and its trigram indexes.

1. Post-load SQL checks (../sql/checks/postload.sql), each must return no rows. Each is timed.
2. Trigram indexes: DHIS2's TRACKER_SEARCH_OPTIMIZATION job builds one for each attribute
   its parameters list that is unique, or searchable in a program or tracked entity type. verify
   lists those the metadata marks trigramIndexable, a field DHIS2 2.42 does not store, runs the
   job once and deletes it after: if left scheduled it could run during a perf test. Then it
   checks each attribute has its index.
3. Importer validation, per program: a sample of the tracked entities enrolled in it is exported
   through /api/tracker with its enrollments and events, and its relationships apart (a workaround
   for https://dhis2.atlassian.net/browse/DHIS2-21405), given fresh UIDs, and submitted with
   importMode=VALIDATE and importStrategy=CREATE: the importer's full create validation and every
   program rule run, without writing (importer.py). The values of unique attributes are left out
   of the copies, which would otherwise repeat them (E1064), and those of the spec's unvalidated
   fields, which a rule assigns from what the importer cannot reproduce for a record of the past.
   Any error fails: the spec must satisfy the rules, which `dbgen validate` checks on a small area
   before a build.
4. Smoke test: tracker API queries of the kinds the performance tests make, for each program, as
   admin and as a clerk of the busiest org unit, and for a PROTECTED program that a record owned
   by another org unit stays hidden from the clerk. It reads only.

Results go to output/stage/<build>/verify/results.json. The stack is stopped at the end.
"""

import json
import sys
import time

import httpx

from dbgen import compile as compile_stage
from dbgen import provenance, sqlfile, timing
from dbgen.config import Build, listed
from dbgen.dhis2 import client, importer
from dbgen.errors import InternalError, UsageError
from dbgen.uid import uid

SAMPLE_SIZE = 200
EXPORT_FIELDS = "*,!relationships,enrollments[*,!relationships,events[*,!relationships]]"
SMOKE_TIMEOUT = 600
CHUNK = 25
TRIGRAM_JOB = {
    "name": "Tracker trigram index maintenance",
    "jobType": "TRACKER_SEARCH_OPTIMIZATION",
}
TRIGRAM_TIMEOUT = 3600


def run(build: Build) -> dict:
    effective, parents = compile_stage.inputs(build, "verify")
    metadata = compile_stage.current_metadata(effective)
    trigram_attributes = {
        a["id"] for a in metadata.of("trackedEntityAttributes") if a.get("trigramIndexable")
    }
    with timing.timed(build.dir, "verify"):
        with client.running(build.project, ("db",), timeout=300):
            with client.connect(build.project, autocommit=True) as pg, pg.cursor() as cur:
                provenance.check_database(cur, provenance.bootstrap_run(parents["load"]))
                checks, check_seconds = postload_checks(cur)
                samples = {p["uid"]: _sample(cur, p["uid"]) for p in effective["programs"]}
            client.start(build.project, ("web", "web-healthcheck"))
            with (
                client.api(build.project) as api,
                client.connect(build.project, autocommit=True) as pg,
                pg.cursor() as cur,
            ):
                trigram = trigram_indexes(api, cur, trigram_attributes)
                known = importer.names(cur)
                skip = unique_attributes(cur) | importer.unvalidated(effective)
                validation = {
                    p["key"]: importer_validation(api, p["uid"], samples[p["uid"]], known, skip)
                    for p in effective["programs"]
                }
                smoke_results = smoke(api, cur, effective, metadata)
    result = {
        "postload": checks,
        "postload_seconds": check_seconds,
        "trigram": trigram,
        "importer": validation,
        "smoke": smoke_results,
    }
    output = build.make_stage_dir("verify")
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    failures = [f"post-load check {name}: {n} row(s)" for name, n in checks.items() if n]
    failures += [f"trigram index missing on attribute {a}" for a in trigram["missing"]]
    refused = []
    for name, report in validation.items():
        refused += [
            f"importer {name}: {e['count']} x {e['code']} rule {e['rule']} field {e['field']}: "
            f"{e['example'][:200]}"
            for e in report["errors"][:20]
        ]
    failures += [f"smoke: {r}" for r in smoke_results if not r["ok"]]
    slowest = dict(sorted(check_seconds.items(), key=lambda kv: -kv[1])[:5])
    print(
        f"verify: {len(checks)} post-load checks, slowest {listed(slowest, 's')}", file=sys.stderr
    )
    print(f"  trigram index job: ran in {trigram['seconds']}s", file=sys.stderr)
    for name, report in validation.items():
        print(f"  importer {name}: validated {report['exported']}", file=sys.stderr)
        print(report["summary"], file=sys.stderr)
    for r in smoke_results:
        print(
            f"  {r['seconds']:7.3f}s {r['status']} {r['items']:4d} {r['user']:5s} {r['query']}",
            file=sys.stderr,
        )
    if failures:
        raise InternalError(
            f"verify found {len(failures)} problem(s) in the loaded database:\n  "
            + "\n  ".join(failures)
        )
    if refused:
        raise UsageError(
            "DHIS2's importer refuses records of the sample, so the spec must change; dbgen "
            "validate over a small area names the rules they break:\n  " + "\n  ".join(refused)
        )
    provenance.record(build, "verify", parents, effective)
    return result


def postload_checks(cur) -> tuple[dict[str, int], dict[str, float]]:
    """The rows each post-load check finds, and the seconds it takes."""
    counts, seconds = {}, {}
    for name, query in sqlfile.checks(sqlfile.SQL / "checks" / "postload.sql").items():
        start = time.perf_counter()
        cur.execute(f"select count(*) from ({query}) q")
        counts[name] = cur.fetchone()[0]
        seconds[name] = round(time.perf_counter() - start, 2)
    return counts, seconds


def trigram_indexes(api: httpx.Client, cur, attributes: set[str]) -> dict:
    """Run DHIS2's trigram index job once, wait for it and delete it, so the dump holds no job.
    Returns the seconds it took and the attributes without a valid index, which the job names
    in_gin_teavalue_<id>."""
    start = time.perf_counter()
    response = api.get(
        "/api/jobConfigurations",
        params={"filter": f"name:eq:{TRIGRAM_JOB['name']}", "fields": "id"},
    )
    response.raise_for_status()
    for job in response.json()["jobConfigurations"]:
        api.delete(f"/api/jobConfigurations/{job['id']}").raise_for_status()
    config = {**TRIGRAM_JOB, "jobParameters": {"attributes": sorted(attributes)}}
    response = api.post("/api/jobConfigurations", json=config)
    response.raise_for_status()
    job = response.json()["response"]["uid"]
    try:
        api.post(f"/api/jobConfigurations/{job}/execute").raise_for_status()
        while True:
            time.sleep(1)
            response = api.get(
                f"/api/jobConfigurations/{job}",
                params={"fields": "jobStatus,lastExecutedStatus,lastFinished"},
            )
            response.raise_for_status()
            status = response.json()
            if status.get("lastFinished") and status["jobStatus"] != "RUNNING":
                break
            if time.perf_counter() - start > TRIGRAM_TIMEOUT:
                raise InternalError(f"trigram index job {job} took over {TRIGRAM_TIMEOUT} s")
    finally:
        api.delete(f"/api/jobConfigurations/{job}").raise_for_status()
    if status["lastExecutedStatus"] != "COMPLETED":
        raise InternalError(f"trigram index job {job} ended {status['lastExecutedStatus']}")
    seconds = round(time.perf_counter() - start, 1)
    cur.execute(
        """
        select tea.uid
        from trackedentityattribute tea
        join pg_index i
          on i.indexrelid = to_regclass('in_gin_teavalue_' || tea.trackedentityattributeid)
        where tea.uid = any(%s)
          and i.indisvalid
        """,
        [sorted(attributes)],
    )
    indexed = {row[0] for row in cur.fetchall()}
    return {"seconds": seconds, "missing": sorted(attributes - indexed)}


def _sample(cur, program: str) -> list[str]:
    """SAMPLE_SIZE tracked entities enrolled in the program and not deleted, spread evenly over
    them; all of them when there are fewer."""
    cur.execute(
        """
        select te.uid from trackedentity te
        where not te.deleted and te.trackedentityid in (
            select en.trackedentityid from enrollment en
            join program p using (programid) where p.uid = %s)
        order by te.trackedentityid
        """,
        [program],
    )
    uids = [u for (u,) in cur.fetchall()]
    if len(uids) <= SAMPLE_SIZE:
        return uids
    return [uids[k * len(uids) // SAMPLE_SIZE] for k in range(SAMPLE_SIZE)]


def unique_attributes(cur) -> set[str]:
    cur.execute("select uid from trackedentityattribute where uniquefield")
    return {u for (u,) in cur.fetchall()}


def importer_validation(
    api: httpx.Client, program: str, uids: list[str], known: dict, skip: set[str]
) -> dict:
    """Export the sample with its enrollments, events and relationships, give it fresh UIDs and
    submit it to validate (importer.py), without the values of the fields of skip: the unique
    attributes and the spec's unvalidated fields."""
    errors, warnings = importer.Tally(), importer.Tally()
    exported = {"trackedEntities": 0, "enrollments": 0, "events": 0, "relationships": 0}
    for i in range(0, len(uids), CHUNK):
        chunk = uids[i : i + CHUNK]
        response = api.get(
            "/api/tracker/trackedEntities",
            params={
                "trackedEntities": ",".join(chunk),
                "program": program,
                "fields": EXPORT_FIELDS,
                "paging": "false",
            },
        )
        response.raise_for_status()
        body = response.json()
        tracked_entities = body.get("trackedEntities", body.get("instances", []))
        if len(tracked_entities) != len(chunk):
            raise InternalError(
                f"export returned {len(tracked_entities)} of {len(chunk)} tracked entities"
            )
        exported["trackedEntities"] += len(tracked_entities)
        relationships = {r["relationship"]: r for te in chunk for r in _relationships(api, te)}
        exported["relationships"] += len(relationships)
        old = list(relationships)
        importer.without({"trackedEntities": tracked_entities}, skip)
        for te in tracked_entities:
            old.append(te["trackedEntity"])
            for en in te.get("enrollments", []):
                exported["enrollments"] += 1
                exported["events"] += len(en.get("events", []))
                old.append(en["enrollment"])
                old += [ev["event"] for ev in en.get("events", [])]
        text = json.dumps(
            {"trackedEntities": tracked_entities, "relationships": list(relationships.values())}
        )
        for value in old:
            text = text.replace(f'"{value}"', f'"{uid("verify", value)}"')
        importer.validate(api, json.loads(text), errors, warnings, known)
    return {
        "exported": exported,
        "errors": errors.rows(known),
        "warnings": warnings.rows(known),
        "summary": importer.summary(f"    {program}", errors, warnings, known, limit=10),
    }


def _relationships(api: httpx.Client, trackedentity: str) -> list[dict]:
    response = api.get(
        "/api/tracker/relationships", params={"trackedEntity": trackedentity, "paging": "false"}
    )
    response.raise_for_status()
    return response.json()["relationships"]


def _one(cur, query: str, params: list | None = None):
    cur.execute(query, params or [])
    return cur.fetchone()


def smoke(api: httpx.Client, cur, effective: dict, metadata) -> list[dict]:
    """Tracker queries modelled on core's TrackerTest, per program, at its busiest org unit."""
    queries = []
    for p in effective["programs"]:
        program = p["uid"]
        busiest = _one(
            cur,
            """
            select o.uid, o.organisationunitid from event ev
            join enrollment en using (enrollmentid)
            join program pr using (programid)
            join organisationunit o on o.organisationunitid = ev.organisationunitid
            where pr.uid = %s
            group by 1, 2 order by count(*) desc, 1 limit 1
            """,
            [program],
        )
        if busiest is None:
            continue
        facility, facility_id = busiest
        te, en, ev = _one(
            cur,
            """
            select te.uid, en.uid, ev.uid from event ev
            join enrollment en using (enrollmentid)
            join trackedentity te using (trackedentityid)
            join program pr using (programid)
            where pr.uid = %s and not ev.deleted
            order by ev.eventid limit 1
            """,
            [program],
        )
        searchable = [a for a in p["attributes"] if a["searchable"]]
        text = [a for a in searchable if a["value_type"] == "TEXT" and not a["unique"]]
        name_filter = None
        if text:
            row = _one(
                cur,
                """
                select left(v.value, 3) from trackedentityattributevalue v
                join trackedentityattribute a using (trackedentityattributeid)
                join enrollment en using (trackedentityid)
                join program pr using (programid)
                where a.uid = %s and pr.uid = %s
                order by v.trackedentityid limit 1
                """,
                [text[0]["uid"], program],
            )
            if row:
                name_filter = f"{text[0]['uid']}:like:{row[0]}"
        selected = {"orgUnits": facility, "orgUnitMode": "SELECTED", "order": "createdAt:desc"}
        k = p["key"]
        queries += [
            (
                "admin",
                f"{k} tracked entities SELECTED",
                "/api/tracker/trackedEntities",
                {"program": program, **selected, "pageSize": 15},
                200,
                True,
            ),
            (
                "admin",
                f"{k} active enrollments ACCESSIBLE",
                "/api/tracker/trackedEntities",
                {
                    "program": program,
                    "orgUnitMode": "ACCESSIBLE",
                    "enrollmentStatus": "ACTIVE",
                    "pageSize": 15,
                },
                200,
                None,
            ),
            (
                "admin",
                f"{k} events by occurredAt",
                "/api/tracker/events",
                {
                    "program": program,
                    "orgUnit": facility,
                    "orgUnitMode": "SELECTED",
                    "order": "occurredAt:desc",
                    "pageSize": 50,
                },
                200,
                True,
            ),
            (
                "admin",
                f"{k} events not assigned",
                "/api/tracker/events",
                {
                    "program": program,
                    "orgUnit": facility,
                    "orgUnitMode": "SELECTED",
                    "assignedUserMode": "NONE",
                    "order": "occurredAt:desc",
                    "pageSize": 50,
                },
                200,
                None,
            ),
            (
                "admin",
                f"{k} single tracked entity",
                f"/api/tracker/trackedEntities/{te}",
                {"program": program, "fields": "*,enrollments[*,events[*]]"},
                200,
                None,
            ),
            ("admin", f"{k} single enrollment", f"/api/tracker/enrollments/{en}", {}, 200, None),
            ("admin", f"{k} single event", f"/api/tracker/events/{ev}", {}, 200, None),
            (
                "admin",
                f"{k} relationships of a tracked entity",
                "/api/tracker/relationships",
                {"trackedEntity": te, "paging": "false"},
                200,
                None,
            ),
        ]
        if name_filter:
            queries.append(
                (
                    "admin",
                    f"{k} tracked entities by name",
                    "/api/tracker/trackedEntities",
                    {
                        "program": program,
                        "orgUnitMode": "ACCESSIBLE",
                        "filter": name_filter,
                        "pageSize": 5,
                    },
                    200,
                    None,
                )
            )
        clerk = _one(
            cur,
            """
            select u.username from usermembership m
            join userinfo u using (userinfoid)
            where m.organisationunitid = %s and u.username like %s
            order by u.username limit 1
            """,
            [facility_id, f"{effective['users']['prefix']}.%"],
        )
        if clerk:
            queries += [
                (
                    clerk[0],
                    f"{k} clerk working list",
                    "/api/tracker/trackedEntities",
                    {"program": program, "orgUnitMode": "CAPTURE", "pageSize": 15},
                    200,
                    True,
                ),
                (
                    clerk[0],
                    f"{k} clerk events",
                    "/api/tracker/events",
                    {"program": program, "pageSize": 50, "order": "occurredAt:desc"},
                    200,
                    True,
                ),
            ]
            if p["access_level"] == "PROTECTED":
                other = _one(
                    cur,
                    """
                    select te.uid from trackedentityprogramowner o
                    join program pr using (programid)
                    join trackedentity te using (trackedentityid)
                    where pr.uid = %s and o.organisationunitid <> %s and not te.deleted
                    order by te.trackedentityid limit 1
                    """,
                    [program, facility_id],
                )
                if other:
                    queries.append(
                        (
                            clerk[0],
                            f"{k} clerk tracked entity of another org unit (PROTECTED)",
                            f"/api/tracker/trackedEntities/{other[0]}",
                            {"program": program},
                            None,
                            None,
                        )
                    )
    results = []
    password = effective["users"]["password"]
    clients: dict[str, httpx.Client] = {}
    try:
        for who, name, path, params, expected, non_empty in queries:
            if who == "admin":
                http = api
            else:
                if who not in clients:
                    clients[who] = httpx.Client(
                        base_url=api.base_url, auth=(who, password), timeout=SMOKE_TIMEOUT
                    )
                http = clients[who]
            status, items, seconds = _timed(http, path, params)
            if expected is None:
                # A record of another org unit of a protected program: hidden, whether as 403
                # or 404 depends on whether the clerk may search there.
                ok = status in (403, 404)
            else:
                ok = status == expected and (non_empty is None or (items > 0) == non_empty)
            results.append(
                {
                    "query": name,
                    "user": "admin" if who == "admin" else "clerk",
                    "status": status,
                    "items": items,
                    "seconds": seconds,
                    "ok": ok,
                }
            )
    finally:
        for c in clients.values():
            c.close()
    del metadata
    return results


def _timed(api: httpx.Client, path: str, params: dict) -> tuple[int, int, float]:
    start = time.perf_counter()
    response = api.get(path, params=params)
    elapsed = round(time.perf_counter() - start, 3)
    body = response.json() if response.status_code == 200 and response.content else {}
    items = next((v for v in body.values() if isinstance(v, list)), []) if body else []
    return response.status_code, len(items), elapsed
