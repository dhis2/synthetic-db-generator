"""`dbgen stage users`: the users of a build, and the user groups they join.

Three kinds of user:

* clerks: of every org unit a program registers at (its orgunit_level), as many as the org unit's
  type has (the level's types), else users.clerks. A clerk captures at the org unit, searches and
  views the org unit of users.search_level above it, and creates its records (generate).
* officers: one per org unit of the levels of users.officers that holds an org unit of the area,
  view only.
* an admin, with the ALL authority, at the root.

The groups come from the sharing the metadata ships: of every program, the user groups with data
write access take the clerks, those with data read access only the officers, those with metadata
write access but no data access the admin. A program whose sharing has no group of a kind gives
that kind its clerks' groups, so every user reads the programs it works with. Its tracked entity
type must share data with the clerks' and officers' groups too, as DHIS2 checks both.

A user is named "User" and its number in the order the users are made, so no name is a real
person's. Usernames are lower case, as DHIS2 matches them case-sensitively:
<prefix>.<org unit code>.<n> of a clerk, <prefix>.<level>.<org unit code> of an officer and
<prefix>.admin.

Writes users.parquet, one row per user with each column described in its schema metadata, and
users.json, the metadata payload of the user roles and users, with the members of each group.
bootstrap imports the first user of each kind through the API and copies the others in SQL
(dhis2/user_copies.py).
"""

import json
import sys
from pathlib import Path

import duckdb

from dbgen import compile as compile_stage
from dbgen import parquet, provenance
from dbgen.config import Build, listed
from dbgen.errors import InternalError, UsageError
from dbgen.metadata import Metadata
from dbgen.spec import slug
from dbgen.uid import uid

ROLES = {
    "clerk": {"name": "Tracker data capture", "authorities": ["M_capture", "F_UNCOMPLETE_EVENT"]},
    "officer": {"name": "Tracker view", "authorities": ["M_capture", "F_VIEW_EVENT_ANALYTICS"]},
    "admin": {"name": "Tracker superuser", "authorities": ["ALL"]},
}

COLUMNS = parquet.describe(
    "users",
    {
        "kind": "clerk, officer or admin, which sets the user role and the user groups",
        "uid": "DHIS2 UID, a hash of the username",
        "username": "Lower case: <prefix>.<code>.<n>, <prefix>.<level>.<code> or <prefix>.admin",
        "first_name": "Given name, User",
        "surname": "Family name, the user's number from 01 in the order they are made",
        "orgunit_uid": "UID of the org unit of a clerk, null for the others",
        "capture_uid": "UID of the org unit the user captures data in",
        "search_uid": "UID of the org unit the user searches: a clerk's of users.search_level",
        "view_uid": "UID of the org unit the user views data of, as search_uid",
    },
)


def run(build: Build) -> Path:
    effective, parents = compile_stage.inputs(build, "users")
    provenance.forget(build, "users")
    metadata = compile_stage.current_metadata(effective)
    users = build_users(build, effective)
    output = build.make_stage_dir("users")
    target = output / "users.parquet"
    con = duckdb.connect()
    con.execute("create table user as select unnest(?, recursive := true)", [users])
    parquet.copy(con, "(select * from user order by username)", target, COLUMNS)
    payload = metadata_payload(users, effective, groups(effective, metadata))
    (output / "users.json").write_text(json.dumps(payload, indent=1) + "\n")
    kinds = {k: sum(u["kind"] == k for u in users) for k in ROLES}
    print(f"users: {listed(kinds)}", file=sys.stderr)
    provenance.record(build, "users", parents, effective)
    return target


def build_users(build: Build, effective: dict) -> list[dict]:
    orgunits = build.stage_dir("orgunits") / "orgunits.parquet"
    spec = effective["users"]
    prefix = spec["prefix"]
    levels = [effective["orgunits"]["root"]["name"]] + [
        lv["name"] for lv in effective["orgunits"]["levels"]
    ]
    capture_levels = sorted({levels.index(p["orgunit_level"]) + 1 for p in effective["programs"]})
    search_level = levels.index(spec["search_level"]) + 1
    officer_levels = [levels.index(name) + 1 for name in spec["officers"]]
    con = duckdb.connect()
    con.execute("create table ou as select * from read_parquet(?) where kept", [str(orgunits)])
    [(root,)] = con.execute("select uid from ou where level = 1").fetchall()
    # Each org unit of a capture level, with its ancestor of the search level.
    clerks_at = con.execute(
        f"""
        select o.uid, o.code, coalesce(o.clerks, {int(spec["clerks"])}),
               coalesce(s.uid, o.uid)
        from ou o
        left join ou s on s.level = {search_level}
          and list_contains(string_split(o.path, '/'), s.code)
        where o.level in (select unnest(?))
        order by o.code
        """,
        [capture_levels],
    ).fetchall()
    # Every org unit of an officer level above a capture org unit.
    officers_at = con.execute(
        """
        select distinct a.uid, a.code, a.level_name
        from ou o
        join ou a on list_contains(string_split(o.path, '/'), a.code)
        where o.level in (select unnest(?)) and a.level in (select unnest(?))
        order by a.level_name, a.code
        """,
        [capture_levels, officer_levels],
    ).fetchall()
    users: list[dict] = []

    def add(kind, username, capture, view, orgunit=None):
        users.append(
            {
                "kind": kind,
                "uid": uid("user", username),
                "username": username,
                "first_name": "User",
                # DHIS2 takes a surname of 2 characters or more.
                "surname": f"{len(users) + 1:02d}",
                "orgunit_uid": orgunit,
                "capture_uid": capture,
                "search_uid": view,
                "view_uid": view,
            }
        )

    for ou_uid, code, n, search in clerks_at:
        for i in range(1, n + 1):
            add("clerk", f"{prefix}.{code.lower()}.{i}", ou_uid, search, orgunit=ou_uid)
    for ou_uid, code, level_name in officers_at:
        add("officer", f"{prefix}.{slug(level_name)}.{code.lower()}", ou_uid, ou_uid)
    add("admin", f"{prefix}.admin", root, root)
    check_uids(users)
    return users


def check_uids(users: list[dict]) -> None:
    """Fail on users sharing a UID: a hash of the username, so two users sharing a username share
    a UID too; two usernames sharing one are a collision of the hash."""
    usernames: dict[str, list[str]] = {}
    for u in users:
        usernames.setdefault(u["uid"], []).append(u["username"])
    shared = [f"{u} ({', '.join(names)})" for u, names in usernames.items() if len(names) > 1]
    if shared:
        raise InternalError("users sharing a UID: " + ", ".join(sorted(shared)))


def groups(effective: dict, metadata: Metadata) -> dict[str, list[str]]:
    """The user groups of each kind of user, from the sharing of the programs (see the module
    docstring)."""
    kinds: dict[str, list[str]] = {"clerk": [], "officer": [], "admin": []}
    for p in effective["programs"]:
        program = metadata.get("programs", p["uid"]) or {}
        found = {"clerk": [], "officer": [], "admin": []}
        for group, access in ((program.get("sharing") or {}).get("userGroups") or {}).items():
            a = access.get("access", "--------")
            if metadata.user_group(group) is None:
                raise UsageError(
                    f"program {p['name']} is shared with user group {group}, which the metadata "
                    "does not ship"
                )
            if a[3:4] == "w":
                found["clerk"].append(group)
            elif a[2:3] == "r":
                found["officer"].append(group)
            elif a[1:2] == "w":
                found["admin"].append(group)
        if not found["clerk"]:
            raise UsageError(
                f"program {p['name']}: no user group of its sharing may write data, so no user "
                "could capture it; share it with one in the metadata"
            )
        for kind in ("officer", "admin"):
            if not found[kind]:
                found[kind] = found["clerk"]
        check_type_sharing(p["name"], program, metadata, found)
        for kind, gs in found.items():
            for g in gs:
                if g not in kinds[kind]:
                    kinds[kind].append(g)
    return kinds


def check_type_sharing(name: str, program: dict, metadata: Metadata, found: dict) -> None:
    """Fail on a program whose tracked entity type does not give its clerks' groups data write
    or its officers' groups data read: DHIS2 checks both, so its users could not capture or read
    its tracked entities."""
    tet = metadata.get("trackedEntityTypes", program["trackedEntityType"]["id"]) or {}
    shared = (tet.get("sharing") or {}).get("userGroups") or {}

    def access(group):
        return (shared.get(group) or {}).get("access", "--------")

    lacking = [f"{g} (data write)" for g in found["clerk"] if access(g)[3:4] != "w"]
    lacking += [f"{g} (data read)" for g in found["officer"] if access(g)[2:3] != "r"]
    if lacking:
        raise UsageError(
            f"program {name}: its tracked entity type {tet.get('name', '?')} does not share "
            f"with user groups {', '.join(lacking)} as the program does; share it with them in "
            "the metadata"
        )


def metadata_payload(users: list[dict], effective: dict, kinds: dict[str, list[str]]) -> dict:
    password = effective["users"]["password"]
    roles = [
        {"id": uid("role", kind), "name": role["name"], "authorities": role["authorities"]}
        for kind, role in ROLES.items()
    ]
    payload_users = [
        {
            "id": u["uid"],
            "username": u["username"],
            "firstName": u["first_name"],
            "surname": u["surname"],
            "password": password,
            "userRoles": [{"id": uid("role", u["kind"])}],
            "organisationUnits": [{"id": u["capture_uid"]}],
            "teiSearchOrganisationUnits": [{"id": u["search_uid"]}],
            "dataViewOrganisationUnits": [{"id": u["view_uid"]}],
        }
        for u in users
    ]
    members: dict[str, list[dict]] = {}
    for u in users:
        for group in kinds[u["kind"]]:
            members.setdefault(group, []).append({"id": u["uid"]})
    return {"userRoles": roles, "users": payload_users, "userGroupMembers": members}
