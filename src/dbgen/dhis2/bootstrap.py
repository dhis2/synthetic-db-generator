"""`dbgen stage bootstrap`: DHIS2 on an empty database, the metadata through the API, ID maps.

1. Start Postgres and DHIS2 (docker/compose.yaml). Flyway creates the schema on first start.
2. Set the system settings that say the data is made up, import the org unit levels and a few
   org units (orgunit_copies.py), then the metadata files as they are, in the spec's order, then
   the user roles and the users (of each kind only the first).
3. Add what depends on the build, and so cannot be in the metadata: the users to the user groups
   the metadata ships (users.py), and the root to the built-in admin, whom the API calls log in
   as.
4. Stop DHIS2, and in SQL copy the other org units of each level from one the API imported
   (orgunit_copies.py), assign the programs to the org units of the build that they register at,
   and copy the other users of each kind from its first (user_copies.py).
5. Export the internal IDs DHIS2 assigned, and the metadata the checks read, as ID maps to
   output/stage/<build>/bootstrap/<table>.parquet, and mark the database with the run (see
   provenance.py).
6. Stop the stack. `load` starts Postgres again on the same volume.

A metadata file that DHIS2 refuses fails the build with DHIS2's errors: making it import is not
the generator's job. The generator takes metadata as an implementer exports it from a running
instance, and never changes it.
"""

import json
import sys

import duckdb
import httpx
import psycopg

from dbgen import compile as compile_stage
from dbgen import parquet, provenance, timing
from dbgen.config import Build, listed
from dbgen.dhis2 import client, orgunit_copies, user_copies
from dbgen.errors import InternalError, UsageError

# The ID maps, the query of each by its name: load reads each as table b_<name>, to swap the
# UIDs of generate's tables for internal IDs. Each query names its columns, so a column DHIS2
# renames fails here.
IDMAP_QUERIES = {
    "program": (
        "select programid, uid, trackedentitytypeid, categorycomboid, accesslevel from program"
    ),
    "programstage": (
        "select programstageid, uid, name, repeatable, programid, validationstrategy "
        "from programstage"
    ),
    "programstagedataelement": (
        "select programstageid, dataelementid, compulsory from programstagedataelement"
    ),
    "dataelement": "select dataelementid, uid, code, name, valuetype, optionsetid from dataelement",
    "optionset": "select optionsetid, uid from optionset",
    "optionvalue": "select optionvalueid, uid, code, optionsetid from optionvalue",
    "trackedentityattribute": (
        "select trackedentityattributeid, uid, code, name, valuetype, optionsetid, uniquefield "
        "from trackedentityattribute"
    ),
    "program_attributes": (
        "select programid, trackedentityattributeid, mandatory from program_attributes"
    ),
    "trackedentitytype": "select trackedentitytypeid, uid from trackedentitytype",
    "trackedentitytypeattribute": (
        "select trackedentitytypeid, trackedentityattributeid from trackedentitytypeattribute"
    ),
    "organisationunit": (
        "select organisationunitid, uid, code, path, hierarchylevel from organisationunit"
    ),
    "program_organisationunits": (
        "select programid, organisationunitid from program_organisationunits"
    ),
    "relationshiptype": "select relationshiptypeid, uid, bidirectional from relationshiptype",
    "userinfo": "select userinfoid, uid, username, firstname, surname from userinfo",
    "categoryoptioncombo": (
        "select coc.categoryoptioncomboid, coc.uid, coc.name, cc.categorycomboid "
        "from categoryoptioncombo coc "
        "left join categorycombos_optioncombos cc using (categoryoptioncomboid)"
    ),
}

IDMAP_COLUMNS = {
    "program": {
        "programid": "Internal ID of the program",
        "uid": "UID of the program",
        "trackedentitytypeid": "Internal ID of the tracked entity type it enrolls",
        "categorycomboid": "Internal ID of its category combo, of its events' option combos",
        "accesslevel": "OPEN, AUDITED, PROTECTED or CLOSED",
    },
    "programstage": {
        "programstageid": "Internal ID of the program stage",
        "uid": "UID of the program stage",
        "name": "Name of the program stage",
        "repeatable": "Whether an enrollment can have more than one event of it",
        "programid": "Internal ID of its program",
        "validationstrategy": (
            "When DHIS2 validates its events: ON_COMPLETE or ON_UPDATE_AND_INSERT"
        ),
    },
    "programstagedataelement": {
        "programstageid": "Internal ID of the program stage",
        "dataelementid": "Internal ID of a data element of the program stage",
        "compulsory": "Whether a completed event of the program stage needs a value of it",
    },
    "dataelement": {
        "dataelementid": "Internal ID of the data element",
        "uid": "UID of the data element",
        "code": "Code of the data element, null without one",
        "name": "Name of the data element",
        "valuetype": "Value type of the data element, such as NUMBER",
        "optionsetid": "Internal ID of its option set, null without one",
    },
    "optionset": {
        "optionsetid": "Internal ID of the option set",
        "uid": "UID of the option set",
    },
    "optionvalue": {
        "optionvalueid": "Internal ID of the option",
        "uid": "UID of the option",
        "code": "Code of the option, the value DHIS2 stores",
        "optionsetid": "Internal ID of its option set",
    },
    "trackedentityattribute": {
        "trackedentityattributeid": "Internal ID of the attribute",
        "uid": "UID of the attribute",
        "code": "Code of the attribute, null without one",
        "name": "Name of the attribute",
        "valuetype": "Value type of the attribute, such as TEXT",
        "optionsetid": "Internal ID of its option set, null without one",
        "uniquefield": "Whether no two tracked entities can share a value of it",
    },
    "program_attributes": {
        "programid": "Internal ID of the program",
        "trackedentityattributeid": "Internal ID of an attribute of the program",
        "mandatory": "Whether an enrollment in the program needs a value of it",
    },
    "trackedentitytype": {
        "trackedentitytypeid": "Internal ID of the tracked entity type",
        "uid": "UID of the tracked entity type",
    },
    "trackedentitytypeattribute": {
        "trackedentitytypeid": "Internal ID of the tracked entity type",
        "trackedentityattributeid": "Internal ID of an attribute of the type",
    },
    "organisationunit": {
        "organisationunitid": "Internal ID of the org unit",
        "uid": "UID of the org unit",
        "code": "Code of the org unit, as orgunits.parquet has it",
        "path": "UIDs of the org unit and those above it, from the root, each after a /",
        "hierarchylevel": "Level of the org unit, 1 for the root",
    },
    "program_organisationunits": {
        "programid": "Internal ID of the program",
        "organisationunitid": "Internal ID of an org unit the program is assigned to",
    },
    "relationshiptype": {
        "relationshiptypeid": "Internal ID of the relationship type",
        "uid": "UID of the relationship type",
        "bidirectional": "Whether both sides of a relationship of the type see it",
    },
    "userinfo": {
        "userinfoid": "Internal ID of the user",
        "uid": "UID of the user",
        "username": "Username",
        "firstname": "Given name of the user",
        "surname": "Family name of the user",
    },
    "categoryoptioncombo": {
        "categoryoptioncomboid": "Internal ID of the category option combo",
        "uid": "UID of the category option combo",
        "name": "Name of the category option combo",
        "categorycomboid": "Internal ID of its category combo",
    },
}


def run(build: Build) -> None:
    effective, parents = compile_stage.inputs(build, "bootstrap")
    provenance.forget(build, "bootstrap")
    run_id = provenance.new_run()
    with timing.timed(build.dir, "bootstrap"):
        orgunits = json.loads((build.stage_dir("orgunits") / "orgunits.json").read_text())
        users = json.loads((build.stage_dir("users") / "users.json").read_text())
        api_users, copied_users = user_copies.split(build, users)
        api_orgunits, copied_orgunits = orgunit_copies.split(orgunits, api_users["users"])
        with client.running(build.project):
            _import(build.project, effective, api_orgunits, api_users)
            client.stop(build.project, ("web", "web-healthcheck"))
            # One transaction, committed when the connection closes.
            with client.connect(build.project) as pg, pg.cursor() as cur:
                orgunit_copies.copy(cur, copied_orgunits)
                assign_programs(build, effective, cur)
                user_copies.copy(cur, copied_users)
            export_idmaps(build)
            with client.connect(build.project, autocommit=True) as pg, pg.cursor() as cur:
                provenance.mark_database(cur, build, run_id)
    provenance.record(build, "bootstrap", parents, effective, run=run_id)


def _import(project: str, effective: dict, orgunits: dict, users: dict) -> None:
    root_name = effective["orgunits"]["root"]["name"]
    settings = {
        "applicationTitle": f"{root_name} (synthetic)",
        "keyApplicationIntro": (
            f"{root_name} is made up. Its data is synthetic, generated by dbgen for DHIS2 "
            "performance tests: no person, place or health facility in it is real."
        ),
    }
    with client.api(project) as api:
        api.post("/api/systemSettings", json=settings).raise_for_status()
        admin_uid = api.get("/api/me", params={"fields": "id"}).json()["id"]
        import_metadata(api, orgunits, "org units")
        for entry in effective["build"]["metadata"]:
            payload = json.loads(open(entry["path"], "rb").read())
            import_metadata(api, payload, entry["path"])
        import_metadata(api, {"userRoles": users["userRoles"], "users": users["users"]}, "users")
        add_group_members(api, users["userGroupMembers"])
        root = orgunits["organisationUnits"][0]["id"]
        assign_admin_scope(api, admin_uid, root)


class MetadataImportError(UsageError):
    """DHIS2 refused metadata: the metadata files need a fix, by hand."""


def import_metadata(api: httpx.Client, payload: dict, label: str) -> None:
    """Commit a metadata payload atomically. Fails with every error found.

    With atomicMode ALL a COMMIT validates everything before writing anything. Only a 409
    carries an import report."""
    params = {
        "atomicMode": "ALL",
        "importReportMode": "ERRORS",
        "importStrategy": "CREATE_AND_UPDATE",
        "importMode": "COMMIT",
    }
    response = api.post("/api/metadata", params=params, json=payload)
    if response.is_success:
        stats = response.json()["response"]["stats"]
        print(f"bootstrap: imported {label}: {listed(stats)}", file=sys.stderr)
        return
    try:
        errors = _errors(response.json().get("response", {}))
    except ValueError:  # not JSON
        errors = []
    errors = errors or [f"HTTP {response.status_code}: {response.text[:2000]}"]
    shown = "\n  ".join(errors[:50])
    raise MetadataImportError(
        f"DHIS2 refused the {label} metadata with {len(errors)} error(s):\n  {shown}\n"
        "The metadata must import into the pinned DHIS2 as it is: fix the file by hand."
    )


def _errors(report: dict) -> list[str]:
    errors = []
    for type_report in report.get("typeReports", []):
        klass = type_report.get("klass", "").rsplit(".", 1)[-1]
        for object_report in type_report.get("objectReports", []):
            for error in object_report.get("errorReports", []):
                errors.append(
                    f"{klass} {object_report.get('uid', '')}: {error.get('errorCode')} "
                    f"{error.get('message')}"
                )
    return errors


def add_group_members(api: httpx.Client, members: dict[str, list[dict]]) -> None:
    """Make the users imported through the API members of their user groups. Membership is the
    group's, so it is added to each group, which keeps the rest of the group as the metadata
    ships it. The copies of these users become members in SQL with them."""
    for group, users in members.items():
        for user in (m["id"] for m in users):
            response = api.post(f"/api/userGroups/{group}/users/{user}")
            if not response.is_success:
                raise UsageError(
                    f"adding user {user} to user group {group} failed: HTTP "
                    f"{response.status_code} {response.text[:500]}"
                )
    count = sum(len(u) for u in members.values())
    print(f"bootstrap: {count} memberships of {len(members)} user groups", file=sys.stderr)


def assign_programs(build: Build, effective: dict, cur: psycopg.Cursor) -> None:
    """Assign each program to the org units of its level, as an implementer would.

    In SQL, as most org units are copied in SQL after the API calls (orgunit_copies.py). The
    API's /api/programs/{uid}/organisationUnits writes only these rows."""
    orgunits = build.stage_dir("orgunits") / "orgunits.parquet"
    for p in effective["programs"]:
        uids = [
            u
            for (u,) in duckdb.execute(
                "select uid from read_parquet(?) where imported_to_dhis2 and level_name = ? "
                "order by uid",
                [str(orgunits), p["orgunit_level"]],
            ).fetchall()
        ]
        cur.execute(
            """
            insert into program_organisationunits (programid, organisationunitid)
            select p.programid, o.organisationunitid
            from program p
            join organisationunit o on o.uid = any(%s)
            where p.uid = %s
            """,
            [uids, p["uid"]],
        )
        if cur.rowcount != len(uids):
            raise InternalError(
                f"assigned {p['name']} to {cur.rowcount} of its {len(uids)} org units: the "
                "program or some of its org units are not in the database"
            )
        print(f"bootstrap: {p['key']} assigned to {len(uids)} org units", file=sys.stderr)


def assign_admin_scope(api: httpx.Client, admin_uid: str, root_uid: str) -> None:
    """The built-in admin, whom client.api logs in as, gets the root for capture, search and view.

    A fresh database creates admin before any org unit exists, so it has none, and the tracker
    API then refuses it (E1006)."""
    root = [{"id": root_uid}]
    patch = [
        {"op": "add", "path": f"/{field}", "value": root}
        for field in (
            "organisationUnits",
            "teiSearchOrganisationUnits",
            "dataViewOrganisationUnits",
        )
    ]
    response = api.patch(
        f"/api/users/{admin_uid}",
        content=json.dumps(patch),
        headers={"Content-Type": "application/json-patch+json"},
    )
    response.raise_for_status()


def export_idmaps(build: Build) -> None:
    output = build.make_stage_dir("bootstrap")
    con = duckdb.connect()
    con.execute("install postgres; load postgres")
    con.execute(f"attach '{client.dsn(build.project)}' as pg (type postgres, read_only)")
    counts = {}
    for name, query in IDMAP_QUERIES.items():
        counts[name] = parquet.copy(
            con,
            f"(select * from postgres_query('pg', $${query}$$) order by all)",
            output / f"{name}.parquet",
            parquet.describe("DHIS2", IDMAP_COLUMNS[name]),
        )
    print(f"bootstrap: ID maps {listed(counts)}", file=sys.stderr)
