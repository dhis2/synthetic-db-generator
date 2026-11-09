"""The spec: what the metadata cannot say about the data, laid over what it can.

`compile` reads the metadata, gives every program, stage, data element and attribute a generator
from it (defaults), lays the spec over them, checks the result and writes it as the effective
spec: every field with its generator and where that came from (default or spec).

The spec is TOML. Every table and key is optional but [build].metadata:

    [build]           metadata files in import order, seed, data window (start, end), now
    [orgunits]        the root and the levels of the org unit hierarchy, with their traits
    [trait.<scope>]   traits of a level (by its name in lower case), tracked_entity or enrollment
    [users]           who captures and who views
    [program.<code>]  a program by its code, UID or name: volumes, visits, statuses, stages,
                      values, linking
    [records]         deleted and updated records
    [load]            the order rows are written in

See examples/pregnancy/pregnancy.toml for a small spec, the MNCH spec of
dhis2/synthetic-databases for one that sets them all, and the README for what each key does.
"""

import json
import re
import tomllib
from collections.abc import Iterable
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from dbgen import distribution, expr, sqlfile
from dbgen.errors import UsageError, input_file
from dbgen.metadata import Field, Metadata, Program, Stage, read

DEFAULT_SEED = 42
DEFAULT_START = date(2020, 1, 1)
DEFAULT_YEARS = 5  # the data window's, from its start, without an end
# The made-up country of every spec that names none: Perf Land, whose root the ANC and EIR
# metadata's charts reference.
DEFAULT_ROOT = {"name": "Perf Land", "code": "QP"}
DEFAULT_LEVELS = [
    {"name": "Region", "count": 2},
    {"name": "District", "per_parent": 2},
    {"name": "Facility", "per_parent": 3},
]
DEFAULT_PASSWORD = "Perf-Test-2026!"

# Defaults of a program the spec leaves out, and of the keys it leaves out of one.
DEFAULT_TRACKED_ENTITIES = 100  # per org unit of the program's level
DEFAULT_ENROLLMENTS = {"constant": 1}
DEFAULT_STATUS = {"ACTIVE": 1}
DEFAULT_EVENT_STATUS = {"COMPLETED": 1}
DEFAULT_REPEATS = {"poisson": 1, "min": 1, "max": 10}  # events of a repeatable stage
DEFAULT_SPACING_DAYS = 30  # between events of a repeatable stage without standardInterval
DEFAULT_GAP = {"uniform": [28, 364]}  # days between enrollments of a tracked entity

ENROLLMENT_STATUSES = ("ACTIVE", "COMPLETED", "CANCELLED")
EVENT_STATUSES = ("ACTIVE", "COMPLETED", "SCHEDULE", "SKIPPED")
# Value types with no default: a compulsory one needs a value in the spec.
EMPTY_VALUE_TYPES = (
    "FILE_RESOURCE",
    "IMAGE",
    "COORDINATE",
    "GEOJSON",
    "TRACKER_ASSOCIATE",
    "REFERENCE",
    "MULTI_TEXT",
)

KEYS = {
    "": {"build", "orgunits", "trait", "users", "program", "records", "load"},
    "build": {"metadata", "seed", "start", "end", "now"},
    "orgunits": {"root", "level", "seed"},
    "level": {"name", "count", "per_parent", "types", "per", "clerks"},
    "type": {"name", "per_100k", "urban_factor", "weight", "clerks", "count"},
    "users": {"prefix", "password", "clerks", "officers", "search_level"},
    "program": {
        "orgunit_level",
        "tracked_entities",
        "enrollments",
        "visits",
        "stage",
        "attributes",
        "values",
        "from",
        "trait",
        "unvalidated",
    },
    "enrollments": {"count", "gap", "status", "followup"},
    "visits": {"stages", "count", "spacing", "status"},
    "stage": {"count", "share", "at", "offset", "spacing", "status", "assigned"},
    "tracked_entities": {"per_orgunit", "total", "among", "by"},
    "from": {"enrollments", "where", "share", "relationship", "attributes", "at"},
    "records": {"deleted", "updated", "entry_delay"},
    "updated": {"share", "after"},
    "load": {"order"},
}

TRAIT_SCOPES_RECORD = ("tracked_entity", "enrollment")
# The scopes of expressions besides the levels and a linked program's source.
SCOPES = (*TRAIT_SCOPES_RECORD, "event", "record", "value", "orgunit")
# A trait's name: what an expression can name after the scope (expr.REFERENCE).
TRAIT_NAME = re.compile(r"[a-z][a-z0-9_]*")
# The columns dbgen gives a scope itself, which a trait of it must not be named as: of a level
# (orgunits, Program.place), a tracked entity and an enrollment (generate), and a linked program's
# source besides its traits (Program.source_columns). test_spec checks them against the SQL.
OWN_COLUMNS = {
    "level": ("code", "name", "uid", "type", "weight"),
    "tracked_entity": ("number", "seq"),
    "enrollment": ("date", "number", "status"),
    "source": ("number", "date", "end", "status"),
}
# The generators of no parameters, written { <name> = true }.
FLAGS = ("letter", "phone", "email", "url", "orgunit", "username", "unique")
# The generators that are no distribution (generate.SPECIAL), so take no min, max, round or plus.
SPECIAL = ("words", "pattern", *FLAGS)


def compile_spec(path: Path, codes: tuple[str, ...] = ()) -> tuple[dict, Metadata]:
    """The effective spec of the spec file, and the metadata it names."""
    raw = load_toml(path)
    _check_keys(raw, "", "")
    base = path.parent
    build = _build(_table(raw.get("build"), "build"), base, path)
    metadata = read(Path(m["path"]) for m in build["metadata"])
    trait_tables = _table(raw.get("trait"), "trait")
    orgunits = _orgunits(_table(raw.get("orgunits"), "orgunits"), trait_tables)
    traits = _record_traits(trait_tables)
    users = _users(_table(raw.get("users"), "users"), path.stem, orgunits)
    programs = _programs(_table(raw.get("program"), "program"), metadata, orgunits, traits)
    records = _records(_table(raw.get("records"), "records"))
    load = _table(raw.get("load"), "load")
    _check_keys(load, "load", "load")
    order = load.get("order", "created")
    if order not in ("created", "parallel"):
        raise UsageError("load.order: must be created or parallel")
    effective = {
        "name": path.stem,
        "spec": str(path),
        "codes": list(codes),
        "build": build,
        "orgunits": orgunits,
        "traits": traits,
        "users": users,
        "programs": programs,
        "records": records,
        "load": {"order": order},
        # Per program, the fields validate and verify leave out of what they submit to the
        # importer: kept apart from the programs, as they change no record (provenance.PARTS).
        "unvalidated": {p["key"]: p.pop("unvalidated") for p in programs},
    }
    _check_linking(effective, metadata)
    return effective, metadata


def load_toml(path: Path) -> dict:
    """The spec at path as TOML, failing on what is not a TOML text file."""
    input_file(path, "spec")
    try:
        return tomllib.loads(path.read_text())
    except UnicodeDecodeError as e:
        raise UsageError(f"spec {path} is not UTF-8 text: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"spec {path}: {e}") from e


def _check_keys(table: dict, kind: str, where: str) -> None:
    """Fail on a key of the table that a table of its kind (KEYS) does not take."""
    unknown = sorted(set(table) - KEYS[kind])
    if unknown:
        raise UsageError(
            f"{where or 'spec'}: unknown key {', '.join(unknown)}; known: "
            f"{', '.join(sorted(KEYS[kind]))}"
        )


def _build(table: dict, base: Path, spec_path: Path) -> dict:
    """[build] with its defaults: the metadata files, resolved against base, the spec's
    directory, the seed and the window."""
    _check_keys(table, "build", "build")
    files = table.get("metadata")
    if not files or not isinstance(files, list) or not all(isinstance(file, str) for file in files):
        raise UsageError(
            f"{spec_path}: build.metadata must list the metadata files in import order, e.g. "
            'metadata = ["metadata/anc.json"]'
        )
    # A path relative to the spec's directory, as a path in a file is, so the spec reads the same
    # files wherever dbgen runs; it is kept as written (file), for the digests.
    resolved = [{"file": file, "path": str(base / file)} for file in files]
    seed = table.get("seed", DEFAULT_SEED)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise UsageError("build.seed: must be an integer")
    start = _date(table.get("start", DEFAULT_START), "build.start")
    try:
        default_end = start.replace(year=start.year + DEFAULT_YEARS)
    except ValueError:  # Feb 29, which the year of the end does not have
        default_end = start.replace(year=start.year + DEFAULT_YEARS, day=28)
    end = _date(table.get("end", default_end), "build.end")
    if end <= start:
        raise UsageError("build.end: must be after build.start")
    now = _date(table.get("now", end), "build.now")
    if now < start:
        raise UsageError("build.now: must not be before build.start")
    return {
        "metadata": resolved,
        "seed": seed,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "now": now.isoformat(),
    }


def _date(value: Any, where: str) -> date:
    """A date of the spec: a TOML date or a string such as 2020-01-01, not a date and time."""
    if isinstance(value, datetime):
        raise UsageError(f"{where}: {value} is a date and time; give a date such as 2020-01-01")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise UsageError(f"{where}: {value!r} is not a date such as 2020-01-01")


def _table(value: Any, where: str) -> dict:
    """A table of the spec, {} where the spec leaves it out."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UsageError(f"{where}: must be a table")
    return value


def _orgunits(table: dict, traits: dict) -> dict:
    """[orgunits] with its defaults: the root and the levels below it, each with its traits of
    [trait.<level>]."""
    _check_keys(table, "orgunits", "orgunits")
    root = table.get("root", DEFAULT_ROOT)
    if not isinstance(root, dict) or set(root) - {"name", "code"} or "name" not in root:
        raise UsageError("orgunits.root: must be { name = n, code = c }")
    root = {"name": root["name"], "code": root.get("code", root["name"][:2].upper())}
    levels = deepcopy(table.get("level", DEFAULT_LEVELS))
    if not isinstance(levels, list) or not levels:
        raise UsageError("orgunits.level: must be a list of levels below the root")
    names = [slug(root["name"])]
    out = []
    for i, level in enumerate(levels):
        where = f"orgunits.level[{i}]"
        level = _table(level, where)
        _check_keys(level, "level", where)
        if "name" not in level:
            raise UsageError(f"{where}: needs a name")
        name = level["name"]
        if slug(name) in names or slug(name) in SCOPES:
            raise UsageError(f"{where}: the name {name!r} is taken")
        names.append(slug(name))
        how = [k for k in ("count", "per_parent", "types") if k in level]
        if len(how) != 1:
            raise UsageError(f"{where} {name}: needs one of count, per_parent or types")
        level_traits = dict(_table(traits.get(slug(name)), f"trait.{slug(name)}"))
        _check_traits(level_traits, "level", f"trait.{slug(name)}")
        for trait, t in level_traits.items():
            if isinstance(t, dict) and "largest" in t:
                _check_largest(t, f"trait.{slug(name)}.{trait}")
        entry = {"name": name, "slug": slug(name), "number": i + 2, "traits": level_traits}
        if "count" in level:
            if not _is_int(level["count"]) or level["count"] < 1:
                raise UsageError(f"{where}.count: must be a whole number, 1 or more")
            entry["count"] = level["count"]
        elif "per_parent" in level:
            entry["per_parent"] = level["per_parent"]
        else:
            types = level["types"]
            if not isinstance(types, list) or not types:
                raise UsageError(f"{where}.types: must list the types of the level")
            for t in types:
                if not isinstance(t, dict) or "name" not in t:
                    raise UsageError(f"{where}.types: each needs a name")
                _check_keys(t, "type", f"{where}.types {t['name']}")
                for k in ("per_100k", "urban_factor", "weight"):
                    if k in t and (isinstance(t[k], bool) or not isinstance(t[k], int | float)):
                        raise UsageError(f"{where}.types {t['name']}.{k}: must be a number")
                if "clerks" in t and (not _is_int(t["clerks"]) or t["clerks"] < 1):
                    raise UsageError(
                        f"{where}.types {t['name']}.clerks: must be a whole number, 1 or more"
                    )
            entry["types"] = types
            entry["per"] = level.get("per")
        if "clerks" in level:
            if not _is_int(level["clerks"]) or level["clerks"] < 1:
                raise UsageError(f"{where}.clerks: must be a whole number, 1 or more")
            entry["clerks"] = level["clerks"]
        out.append(entry)
    for i, entry in enumerate(out):
        if "types" in entry:
            per = entry.get("per") or (out[i - 1]["name"] if i else root["name"])
            parents = [lv["name"] for lv in out[:i]]
            if per not in parents and per != root["name"]:
                raise UsageError(
                    f"orgunits.level {entry['name']}.per: {per!r} is not a level above it"
                )
            entry["per"] = per
    unknown_scopes = sorted(set(traits) - set(names) - set(TRAIT_SCOPES_RECORD))
    if unknown_scopes:
        raise UsageError(
            f"trait.{unknown_scopes[0]}: not a level of orgunits ({', '.join(names[1:])}), "
            "tracked_entity or enrollment"
        )
    return {"root": root, "seed": table.get("seed"), "levels": out}


def slug(name: str) -> str:
    """A level's name as a scope of expressions: District -> district, Health Area ->
    health_area."""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _check_traits(traits: dict, kind: str, where: str) -> None:
    """Fail on a trait no expression can name, named as a column dbgen gives its scope, or given
    as a string: a trait is a number, and a string is text, so likely an expression without sql."""
    for name, value in traits.items():
        if isinstance(value, dict) and set(value) == {"constant"}:
            value = value["constant"]
        if isinstance(value, str):
            raise UsageError(
                f"{where}.{name}: a trait is a number, and a string is text; give an expression "
                f"as {{ sql = {json.dumps(value)} }}"
            )
        if not TRAIT_NAME.fullmatch(name):
            raise UsageError(
                f"{where}.{name}: a trait's name holds lowercase letters, digits and _, and starts "
                "with a letter"
            )
        if name in OWN_COLUMNS[kind]:
            raise UsageError(
                f"{where}.{name}: every {kind.replace('_', ' ')} has a {name} of its own; give "
                "the trait another name"
            )


def _check_largest(trait: dict, where: str) -> None:
    """Fail on a level trait of the largest org units that does not say what share they hold."""
    unknown = sorted(set(trait) - {"largest", "until_share"})
    if unknown:
        raise UsageError(f"{where}: unknown key {', '.join(unknown)}; largest takes until_share")
    if "until_share" not in trait:
        raise UsageError(f"{where}: largest needs until_share, the share of the sum they hold")
    _fill(trait["until_share"], f"{where}.until_share")


def _fill(value: Any, where: str) -> float:
    """A share from 0 to 1, such as a field's fill."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise UsageError(f"{where}: must be a share from 0 to 1")
    return float(value)


def _is_int(value) -> bool:
    """Whether a value of the spec is an integer: TOML's true and false are Python's ints too."""
    return isinstance(value, int) and not isinstance(value, bool)


def _record_traits(table: dict) -> dict:
    """The traits of [trait.tracked_entity] and [trait.enrollment], by scope."""
    traits = {}
    for scope in TRAIT_SCOPES_RECORD:
        traits[scope] = dict(_table(table.get(scope), f"trait.{scope}"))
        _check_traits(traits[scope], scope, f"trait.{scope}")
    return traits


def _users(table: dict, spec_name: str, orgunits: dict) -> dict:
    """[users] with its defaults, the prefix the spec's name."""
    _check_keys(table, "users", "users")
    levels = [lv["name"] for lv in orgunits["levels"]]
    officers = table.get("officers", levels[:-1])
    if not isinstance(officers, list) or any(o not in levels for o in officers):
        raise UsageError(f"users.officers: must list levels of orgunits ({', '.join(levels)})")
    search_level = table.get("search_level", levels[-3] if len(levels) >= 3 else levels[0])
    if search_level not in levels:
        raise UsageError(f"users.search_level: {search_level!r} is not a level")
    clerks = table.get("clerks", 1)
    if not _is_int(clerks) or clerks < 1:
        raise UsageError("users.clerks: must be a whole number, 1 or more")
    prefix = table.get("prefix", spec_name)
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", prefix):
        raise UsageError("users.prefix: must be lowercase letters and digits, words joined by '-'")
    return {
        "prefix": prefix,
        "password": table.get("password", DEFAULT_PASSWORD),
        "clerks": clerks,
        "officers": officers,
        "search_level": search_level,
    }


def _programs(table: dict, metadata: Metadata, orgunits: dict, traits: dict) -> list[dict]:
    """Every tracker program of the metadata in its order, each with its [program.<name>] of
    the spec, if any."""
    if events := metadata.event_programs:
        raise UsageError(
            "dbgen generates tracker programs only, and the metadata ships the event programs "
            + ", ".join(f"{p.get('code') or p['id']} ({p['name']})" for p in events)
            + "; leave them out of the metadata"
        )
    programs = metadata.programs
    if not programs:
        raise UsageError("the metadata ships no tracker program")
    by_uid: dict[str, tuple[str, dict]] = {}
    for name, program_table in table.items():
        program_table = _table(program_table, f"program.{name}")
        _check_keys(program_table, "program", f"program.{name}")
        program = find_program(name, metadata, f"program.{name}")
        if program.uid in by_uid:
            raise UsageError(
                f"program.{name}: names {program.name}, as program.{by_uid[program.uid][0]} does"
            )
        by_uid[program.uid] = (name, program_table)
    out = []
    levels = [lv["name"] for lv in orgunits["levels"]]
    taken = {slug(orgunits["root"]["name"]), *map(slug, levels), *SCOPES}
    for program in programs:
        # A program goes by its code, or its UID without one, however the spec names it.
        key = program.code or program.uid
        name, program_table = by_uid.get(program.uid, (key, {}))
        out.append(_program(key, name, program_table, program, metadata, levels, traits, taken))
    # Each program's tables are named after its key (table_suffix): two of one name would mix.
    by_suffix: dict[str, str] = {}
    for p in out:
        other = by_suffix.setdefault(table_suffix(p["key"]), p["key"])
        if other != p["key"]:
            raise UsageError(
                f"programs {other} and {p['key']} have codes that differ only in case or in "
                "characters other than letters, digits and _, so their tables would share a name; "
                "change one code in the metadata"
            )
    return out


def find_program(wanted: str, metadata: Metadata, where: str) -> Program:
    """The program of the metadata a name of the spec names: its UID, else its code, else its
    name, the code and name ignoring case. A name two programs answer to fails."""
    programs = metadata.programs
    for matches in (
        [program for program in programs if program.uid == wanted],
        [program for program in programs if (program.code or "").lower() == wanted.lower()],
        [program for program in programs if program.name.lower() == wanted.lower()],
    ):
        if len(matches) > 1:
            raise UsageError(
                f"{where}: {wanted!r} names several programs: "
                + ", ".join(f"{program.uid} ({program.name})" for program in matches)
                + "; name one by its UID"
            )
        if matches:
            return matches[0]
    raise UsageError(
        f"{where}: no tracker program {wanted!r} in the metadata; it has "
        + ", ".join(f"{program.code or program.uid} ({program.name})" for program in programs)
    )


def _program(
    key: str,
    name: str,
    table: dict,
    program: Program,
    metadata: Metadata,
    levels: list[str],
    traits: dict,
    taken: set[str],
) -> dict:
    """The effective program: the metadata's program, its key, with its table of the spec,
    which names it name, checked and given its defaults."""
    where = f"program.{name}"
    program_traits = _program_traits(table, traits, where)
    orgunit_level = table.get("orgunit_level", levels[-1])
    if orgunit_level not in levels:
        raise UsageError(f"{where}.orgunit_level: {orgunit_level!r} is not a level of orgunits")
    tracked_entities = _tracked_entities(table, levels, orgunit_level, where)
    visits = _visits(table, program, where)
    stage_tables = _stage_tables(table, program, where)
    # The program's fields, for values by code.
    stage_fields = {
        field.uid: metadata.data_element(field.uid) for s in program.stages for field in s.fields
    }
    attribute_uids = dict.fromkeys(
        [a.uid for a in program.attributes] + metadata.type_attributes(program.tracked_entity_type)
    )
    attribute_fields = {uid: metadata.attribute(uid) for uid in attribute_uids}
    overrides = {
        **_overrides(table, "values", stage_fields, attribute_fields, where),
        **_overrides(table, "attributes", attribute_fields, stage_fields, where),
    }
    stages = [
        _stage(s, stage_tables.get(s.uid, {}), visits, stage_fields, overrides, where)
        for s in program.stages
    ]
    attributes = _attributes(program, attribute_fields, overrides, where)
    cocs = metadata.category_option_combos(program.category_combo)
    enrollments, gap, status, followup = _enrollments(table, program, where)
    return {
        "key": key,
        "uid": program.uid,
        "name": program.name,
        "tracked_entity_type": program.tracked_entity_type,
        "access_level": program.access_level,
        "only_enroll_once": program.only_enroll_once,
        "orgunit_level": orgunit_level,
        "tracked_entities": tracked_entities,
        "enrollments": enrollments,
        "gap": gap,
        "status": status,
        "followup": followup,
        "visits": visits,
        "stages": stages,
        "attributes": attributes,
        "category_option_combos": [c["id"] for c in cocs],
        "traits": program_traits,
        "unvalidated": _unvalidated(
            table.get("unvalidated", []),
            [*stage_fields.values(), *attribute_fields.values()],
            where,
        ),
        "from": _from(table.get("from"), where, taken),
    }


def _program_traits(table: dict, traits: dict, where: str) -> dict:
    """The traits of the program's records, by scope: the spec's of all programs, and its own
    over them."""
    own = _table(table.get("trait"), f"{where}.trait")
    unknown = sorted(set(own) - set(TRAIT_SCOPES_RECORD))
    if unknown:
        raise UsageError(
            f"{where}.trait.{unknown[0]}: a program's traits are of "
            f"{' or '.join(TRAIT_SCOPES_RECORD)}"
        )
    program_traits = {}
    for scope in TRAIT_SCOPES_RECORD:
        own_traits = _table(own.get(scope), f"{where}.trait.{scope}")
        _check_traits(own_traits, scope, f"{where}.trait.{scope}")
        program_traits[scope] = {**traits[scope], **own_traits}
    return program_traits


def _tracked_entities(table: dict, levels: list[str], orgunit_level: str, where: str) -> dict:
    """How many tracked entities register: per org unit of the program's level, or a total
    shared among the org units of a level."""
    tracked_entities = table.get("tracked_entities", {})
    if _is_int(tracked_entities):
        tracked_entities = {"per_orgunit": tracked_entities}
    tracked_entities = _table(tracked_entities, f"{where}.tracked_entities")
    _check_keys(tracked_entities, "tracked_entities", f"{where}.tracked_entities")
    if "per_orgunit" in tracked_entities and "total" in tracked_entities:
        raise UsageError(f"{where}.tracked_entities: per_orgunit or total, not both")
    if "total" in tracked_entities:
        among = tracked_entities.get("among")
        if among not in levels[: levels.index(orgunit_level)] and among != orgunit_level:
            above = ", ".join(levels[: levels.index(orgunit_level) + 1])
            raise UsageError(
                f"{where}.tracked_entities.among: names the level whose total is shared, a level "
                f"at or above {orgunit_level}: one of {above}"
            )
    elif "among" in tracked_entities:
        raise UsageError(f"{where}.tracked_entities.among: needs total")
    # A program linked from another draws only the tracked entities its spec asks for besides.
    default_per = (
        0 if "from" in table and "tracked_entities" not in table else DEFAULT_TRACKED_ENTITIES
    )
    return {
        "per_orgunit": tracked_entities.get(
            "per_orgunit", None if "total" in tracked_entities else default_per
        ),
        "total": tracked_entities.get("total"),
        "among": tracked_entities.get("among"),
        "by": tracked_entities.get("by"),
    }


def _visits(table: dict, program: Program, where: str) -> dict | None:
    """The program's visits, stages filled together on one day, or None."""
    if "visits" not in table:
        return None
    visits = _table(table["visits"], f"{where}.visits")
    _check_keys(visits, "visits", f"{where}.visits")
    names = visits.get("stages")
    if not isinstance(names, list) or not names:
        raise UsageError(f"{where}.visits.stages: must list the stages of a visit")
    stages = [find_stage(program, n, f"{where}.visits.stages") for n in names]
    return {
        "stages": [s.uid for s in stages],
        "count": visits.get("count", DEFAULT_REPEATS),
        "spacing": visits.get("spacing", _interval(stages[0])),
        "status": _status(
            visits.get("status"), EVENT_STATUSES, DEFAULT_EVENT_STATUS, f"{where}.visits.status"
        ),
    }


def find_stage(program: Program, name: str, where: str) -> Stage:
    """The program's stage the spec names by UID or name, ignoring case."""
    for s in program.stages:
        if name in (s.uid, s.name) or name.strip().lower() == s.name.lower():
            return s
    raise UsageError(
        f"{where}: no stage {name!r} in program {program.name}; it has "
        + ", ".join(repr(s.name) for s in program.stages)
    )


def _interval(stage: Stage) -> int:
    """The days between a stage's events: its standard interval, or a default."""
    return stage.standard_interval or DEFAULT_SPACING_DAYS


def _status(value: Any, allowed: tuple[str, ...], default: dict, where: str) -> dict:
    """Statuses and their shares, default where the spec leaves them out."""
    value = default if value is None else value
    if not isinstance(value, dict) or not value:
        raise UsageError(f"{where}: must be a table of statuses and their shares")
    bad = [s for s in value if s not in allowed]
    if bad:
        raise UsageError(f"{where}: unknown status {', '.join(bad)}; one of {', '.join(allowed)}")
    if any(isinstance(v, bool) or not isinstance(v, int | float) or v < 0 for v in value.values()):
        raise UsageError(f"{where}: shares must be numbers, 0 or more")
    if sum(value.values()) <= 0:
        raise UsageError(f"{where}: shares sum to 0")
    return dict(value)


def _stage_tables(table: dict, program: Program, where: str) -> dict[str, dict]:
    """The spec's table of each stage it names, by the stage's UID."""
    by_stage = {}
    for stage_name, stage_table in _table(table.get("stage"), f"{where}.stage").items():
        stage = find_stage(program, stage_name, f"{where}.stage")
        stage_table = _table(stage_table, f"{where}.stage.{stage_name}")
        _check_keys(stage_table, "stage", f"{where}.stage.{stage_name}")
        by_stage[stage.uid] = stage_table
    return by_stage


# The program's tables of fields, and what each holds.
FIELDS = {"values": "a data element", "attributes": "an attribute"}


def _overrides(
    table: dict, key: str, fields: dict[str, Field], others: dict[str, Field], where: str
) -> dict[str, tuple[str, Any]]:
    """The generator the spec's table `key`, values or attributes, gives each field it names, by
    the field's UID, with the name it gives the field. A field of the other table fails, naming
    it."""
    other = next(k for k in FIELDS if k != key)
    overrides: dict[str, tuple[str, Any]] = {}
    for name, gen in _table(table.get(key), f"{where}.{key}").items():
        field = _match(name, fields.values(), f"{where}.{key}.{name}")
        if field is None:
            if _match(name, others.values(), f"{where}.{key}.{name}") is not None:
                raise UsageError(
                    f"{where}.{key}.{name}: {FIELDS[other]}; set it in [{where}.{other}]"
                )
            raise UsageError(f"{where}.{key}: {name!r} is not {FIELDS[key]} of the program")
        if field.uid in overrides:
            raise UsageError(f"{where}.{key}.{name}: its field is named twice")
        overrides[field.uid] = (name, gen)
    return overrides


def find_field(name: str, fields: list[Field], where: str) -> Field:
    """A data element or attribute by its code, UID or name."""
    field = _match(name, fields, where)
    if field is None:
        raise UsageError(f"{where}: no data element or attribute {name!r} in the program")
    return field


def _match(name: str, fields: Iterable[Field], where: str) -> Field | None:
    """The field of the UID, else of the code, else of the name, ignoring case. A name two
    fields answer to fails."""
    fields = list(fields)
    for matches in (
        [field for field in fields if field.uid == name],
        [field for field in fields if field.code == name],
        [field for field in fields if field.name.lower() == name.lower()],
    ):
        if len(matches) > 1:
            raise UsageError(
                f"{where}: {name!r} names several fields: "
                + ", ".join(f"{field.uid} ({field.name})" for field in matches)
                + "; name one by its UID"
            )
        if matches:
            return matches[0]
    return None


def _stage(
    stage: Stage,
    table: dict,
    visits: dict | None,
    fields: dict[str, Field],
    overrides: dict[str, tuple[str, Any]],
    where: str,
) -> dict:
    """The effective stage: of the visits, or when its events fall, how many and how often,
    and its values."""
    stage_where = f"{where}.stage.{stage.name}"
    in_visits = visits is not None and stage.uid in visits["stages"]
    entry = {
        "uid": stage.uid,
        "name": stage.name,
        "repeatable": stage.repeatable,
        "in_visits": in_visits,
        "status": _status(
            table.get("status"),
            EVENT_STATUSES,
            visits["status"] if visits and in_visits else DEFAULT_EVENT_STATUS,
            f"{stage_where}.status",
        ),
    }
    entry["assigned"] = _fill(table.get("assigned", 0), f"{stage_where}.assigned")
    if entry["assigned"] > 0 and not stage.user_assignment:
        raise UsageError(
            f"{stage_where}.assigned: the stage does not enable user assignment in the metadata "
            "(enableUserAssignment), and DHIS2 warns on an event assigned in it"
        )
    if in_visits:
        if set(table) - {"status", "assigned"}:
            raise UsageError(f"{stage_where}: a stage of the visits takes only status and assigned")
    else:
        at = table.get("at", "start")
        if at not in ("start", "end"):
            raise UsageError(f"{stage_where}.at: must be start or end")
        entry["at"] = at
        entry["offset"] = table.get("offset", stage.min_days_from_start)
        entry["share"] = _fill(table.get("share", 1), f"{stage_where}.share")
        if stage.repeatable:
            entry["count"] = table.get("count", DEFAULT_REPEATS)
            entry["spacing"] = table.get("spacing", _interval(stage))
        elif "count" in table or "spacing" in table:
            raise UsageError(f"{stage_where}: a stage that does not repeat has no count or spacing")
    entry["values"] = [
        _value(fields[field.uid], field.compulsory, overrides, where, "values")
        for field in stage.fields
    ]
    return entry


def _value(field: Field, required: bool, overrides: dict, where: str, table: str) -> dict:
    """A field's entry: the spec's generator, written if its condition holds (if) at its fill, by
    default always, or the metadata's for a compulsory field the spec leaves alone. Any other
    field stays empty. table: the program's table of the field, values or attributes.

    Its draws are labelled by UID, so that how the spec names it changes none of them; errors
    name it by its key, as the spec does, its code or UID if the spec leaves it alone."""
    where = f"{where}.{table}"
    entry = {
        "uid": field.uid,
        "key": overrides[field.uid][0] if field.uid in overrides else field.code or field.uid,
        "label": f"{table}.{field.uid}",
        "code": field.code,
        "name": field.name,
        "value_type": field.value_type,
        "option_set": field.option_set,
        "unique": field.unique,
        "required": required,
        "fill": 1.0,
    }
    if field.uid in overrides:
        name, gen = overrides[field.uid]
        if isinstance(gen, dict) and "fill" in gen:
            gen = dict(gen)
            entry["fill"] = _fill(gen.pop("fill"), f"{where}.{name}.fill")
            if required and entry["fill"] < 1:
                raise UsageError(f"{where}.{name}.fill: the field is compulsory")
        if isinstance(gen, dict) and "if" in gen:
            gen = dict(gen)
            entry["if"] = gen.pop("if")
            if not isinstance(entry["if"], str) or not entry["if"].strip():
                raise UsageError(f"{where}.{name}.if: must be a DuckDB SQL condition")
        for flag in FLAGS:
            if isinstance(gen, dict) and flag in gen and gen[flag] is not True:
                raise UsageError(f"{where}.{name}.{flag}: takes only true")
        special = [k for k in SPECIAL if isinstance(gen, dict) and k in gen]
        if special and len(gen) > 1:
            others = ", ".join(k for k in gen if k != special[0])
            raise UsageError(
                f"{where}.{name}: {special[0]} takes no {others}; only a distribution "
                f"takes {', '.join(distribution.MODIFIERS)}"
            )
        entry["gen"] = gen
        entry["source"] = "spec"
    else:
        entry["gen"] = None  # filled from the metadata by compile.defaults, if required
        entry["source"] = "default"
        if not required:
            entry["fill"] = 0.0
            entry["says"] = "empty: optional, and the spec sets no value"
    return entry


def _attributes(
    program: Program,
    fields: dict[str, Field],
    overrides: dict[str, tuple[str, Any]],
    where: str,
) -> list[dict]:
    """The effective attributes: the program's and its tracked entity type's."""
    of_program = {a.uid: a for a in program.attributes}
    attributes = []
    for uid, field in fields.items():
        attribute = of_program.get(uid)
        attributes.append(
            _value(field, bool(attribute and attribute.mandatory), overrides, where, "attributes")
            | {
                "in_program": attribute is not None,
                "searchable": bool(attribute and attribute.searchable),
            }
        )
    return attributes


def _enrollments(table: dict, program: Program, where: str) -> tuple[Any, Any, dict, float]:
    """How many enrollments a tracked entity has, the gap between them, their statuses and the
    share of them followed up."""
    enrollments = _table(table.get("enrollments"), f"{where}.enrollments")
    _check_keys(enrollments, "enrollments", f"{where}.enrollments")
    status = _status(
        enrollments.get("status"),
        ENROLLMENT_STATUSES,
        DEFAULT_STATUS,
        f"{where}.enrollments.status",
    )
    count = enrollments.get("count", DEFAULT_ENROLLMENTS)
    if program.only_enroll_once and distribution.kind_of(count) != "constant":
        raise UsageError(
            f"{where}.enrollments.count: {program.name} enrolls a tracked entity only once"
        )
    followup = _fill(enrollments.get("followup", 0), f"{where}.enrollments.followup")
    return count, enrollments.get("gap", DEFAULT_GAP), status, followup


def _unvalidated(names, fields: list[Field], where: str) -> list[str]:
    """The UIDs of the fields of unvalidated: those a rule assigns from what the importer cannot
    reproduce for a record of the past, such as today's date, which validate and verify leave out
    of what they submit."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise UsageError(f"{where}.unvalidated: must list data elements or attributes")
    return [find_field(name, fields, f"{where}.unvalidated").uid for name in names]


def _from(value, where: str, taken: set[str]) -> dict | None:
    """The source of a linked program: the program whose enrollments it reads, and the name its
    expressions call one of them by (as), which must not be a level's or another scope's."""
    if value is None:
        return None
    value = _table(value, f"{where}.from")
    _check_keys(value, "from", f"{where}.from")
    source = value.get("enrollments")
    if (
        not isinstance(source, dict)
        or set(source) != {"of", "as"}
        or not all(isinstance(v, str) for v in source.values())
    ):
        raise UsageError(
            f'{where}.from.enrollments: must be {{ of = "<code>", as = "<name>" }}: the program '
            "whose enrollments it reads, and the name its expressions call one by"
        )
    alias = source["as"]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", alias):
        raise UsageError(f"{where}.from.enrollments.as: must be lowercase letters, digits and _")
    if alias in taken:
        raise UsageError(f"{where}.from.enrollments.as: {alias!r} is a level or a scope already")
    share = _fill(value.get("share", 1), f"{where}.from.share")
    attributes = _table(value.get("attributes"), f"{where}.from.attributes")
    for name in ("where", "at"):
        if name in value and not isinstance(value[name], str):
            raise UsageError(f"{where}.from.{name}: must be an expression")
    return {
        "enrollments": source["of"],
        "as": alias,
        "where": value.get("where"),
        "share": share,
        "relationship": value.get("relationship"),
        "attributes": dict(attributes),
        "at": value.get("at", f"{alias}.end"),
    }


def table_suffix(key: str) -> str:
    """A program's key as the suffix of its tables' names in generate."""
    return re.sub(r"[^a-z0-9_]", "_", key.lower())


def _records(table: dict) -> dict:
    """[records] with its defaults: the shares of records deleted and updated, and the delay of
    their entry."""
    if "assigned" in table:
        raise UsageError(
            'records.assigned: set it on the stage, such as [program.<code>.stage."<name>"] '
            "assigned = 0.05, for the stages whose metadata enables user assignment"
        )
    if "followup" in table:
        raise UsageError(
            "records.followup: set it on the enrollments of a program, such as "
            "[program.<code>.enrollments] followup = 0.03"
        )
    _check_keys(table, "records", "records")
    updated = _table(table.get("updated"), "records.updated")
    _check_keys(updated, "updated", "records.updated")
    return {
        "deleted": _fill(table.get("deleted", 0), "records.deleted"),
        "updated": {
            "share": _fill(updated.get("share", 0), "records.updated.share"),
            "after": updated.get("after", {"exponential": 30}),
        },
        "entry_delay": table.get("entry_delay", 0),
    }


def _check_linking(effective: dict, metadata: Metadata) -> None:
    """Check each linked program's from against its source program, and resolve its
    relationship type and the attributes it copies."""
    by_uid = {p["uid"]: p for p in effective["programs"]}
    for p in effective["programs"]:
        link = p["from"]
        if link is None:
            continue
        where = f"program.{p['key']}.from"
        source = by_uid[find_program(link["enrollments"], metadata, f"{where}.enrollments.of").uid]
        if source is p:
            raise UsageError(
                f"{where}.enrollments.of: {link['enrollments']!r} is the program itself"
            )
        link["enrollments"] = source["key"]
        if source["from"] is not None:
            raise UsageError(f"{where}: {source['key']} is linked from another program itself")
        # The source's traits and own columns are all <as>.<name>: each name must mean one.
        entity_traits, enrollment_traits = (set(source["traits"][s]) for s in TRAIT_SCOPES_RECORD)
        if both := sorted(entity_traits & enrollment_traits):
            raise UsageError(
                f"{where}: {source['key']} has a tracked entity trait and an enrollment trait "
                f"{both[0]}, both {link['as']}.{both[0]}; give one another name"
            )
        if own := sorted((entity_traits | enrollment_traits) & set(OWN_COLUMNS["source"])):
            raise UsageError(
                f"{where}: {source['key']}'s trait {own[0]} would be {link['as']}.{own[0]}, "
                "which is the source enrollment's own; give the trait another name"
            )
        if source["orgunit_level"] != p["orgunit_level"]:
            raise UsageError(
                f"{where}: {p['key']} registers at {p['orgunit_level']}, {source['key']} at "
                f"{source['orgunit_level']}; a linked tracked entity registers where its source "
                "enrolled, so both must register at one level"
            )
        if link["relationship"] is not None:
            types = [
                relationship_type
                for relationship_type in metadata.relationship_types
                if link["relationship"]
                in (
                    relationship_type["id"],
                    relationship_type.get("code"),
                    relationship_type["name"],
                )
            ]
            if not types:
                raise UsageError(
                    f"{where}.relationship: no relationship type {link['relationship']!r} in the "
                    "metadata"
                )
            if len(types) > 1:
                raise UsageError(
                    f"{where}.relationship: {link['relationship']!r} names several relationship "
                    "types: "
                    + ", ".join(f"{t['id']} ({t['name']})" for t in types)
                    + "; name one by its UID"
                )
            relationship_type = types[0]
            ends = [relationship_type.get(side, {}) for side in ("fromConstraint", "toConstraint")]
            if any(e.get("relationshipEntity") != "TRACKED_ENTITY_INSTANCE" for e in ends):
                raise UsageError(
                    f"{where}.relationship: {link['relationship']} must link tracked entities"
                )
            # The programs of its from and to, None where it names none.
            programs = [e.get("program", {}).get("id") for e in ends]
            if programs[0] == p["uid"] or programs[1] == source["uid"]:
                programs.reverse()
                link["relationship_from_source"] = False
            else:
                link["relationship_from_source"] = True
            if programs[0] not in (None, source["uid"]) or programs[1] not in (None, p["uid"]):
                raise UsageError(
                    f"{where}.relationship: {relationship_type['name']} does not link "
                    f"{source['key']} to {p['key']}"
                )
            link["relationship_type"] = relationship_type["id"]
        attributes = {}
        own = {a["uid"]: a for a in p["attributes"]}
        theirs = {a["uid"]: a for a in source["attributes"]}
        # Only copies: a value computed from the source is a generator of the program's values,
        # which read the source's columns, null for a tracked entity no source is linked to.
        hint = (
            f"must be {link['as']}.<attribute of {source['key']}>; to compute a value, give the "
            f"field a generator in [program.{p['key']}.attributes]"
        )
        for name, copied in link["attributes"].items():
            field = find_field(name, [metadata.attribute(u) for u in own], f"{where}.attributes")
            alias = re.escape(link["as"])
            m = re.fullmatch(rf"\s*{alias}\.(.+?)\s*", copied) if isinstance(copied, str) else None
            if m is None:
                raise UsageError(f"{where}.attributes.{name}: {hint}")
            try:
                src = find_field(
                    m.group(1),
                    [metadata.attribute(u) for u in theirs],
                    f"{where}.attributes.{name}",
                )
            except UsageError as e:
                raise UsageError(f"{e}; {name} {hint}") from e
            attributes[field.uid] = {"from": src.uid}
            # A linked tracked entity takes this value; the others the field's generator.
            own[field.uid]["linked"] = attributes[field.uid]
        link["attributes"] = attributes


def apply_defaults(effective: dict, metadata: Metadata) -> list[str]:
    """Give every compulsory field the spec leaves alone its metadata default, and return the
    problems: compulsory fields that get no value, option codes the option set does not hold, and
    strings that are no value of their field, which the spec writes as they are."""
    problems = []
    entries = []
    strings = []
    for p in effective["programs"]:
        for s in p["stages"]:
            entries += [(p, s["name"], v, metadata.data_element(v["uid"])) for v in s["values"]]
        entries += [(p, "attributes", a, metadata.attribute(a["uid"])) for a in p["attributes"]]
    valid_codes = valid_values(
        {
            (field.value_type, o.code)
            for _, _, _, field in entries
            if field.option_set
            for o in metadata.options(field.option_set)
        }
    )
    for p, where, v, field in entries:
        if v["source"] == "default" and not v["required"]:
            continue
        if v["source"] == "default":
            gen, says = default_generator(field, metadata, effective["build"], valid_codes)
            v["gen"] = gen
            v["says"] = says
        else:
            v["says"] = summary(v["gen"])
            if field.option_set and isinstance(v["gen"], dict) and "weights" in v["gen"]:
                codes = {o.code for o in metadata.options(field.option_set)}
                bad = [c for c in v["gen"]["weights"] if str(c) not in codes]
                if bad:
                    problems.append(
                        f"{_path(p, v)}: option codes "
                        f"{bad} are not in its option set, which holds {sorted(codes)}"
                    )
            if isinstance(v["gen"], str):
                strings.append((p, v, field))
        if v["gen"] is None and v["required"]:
            problems.append(
                f"program.{p['key']} {where}: {v['name']} ({v['uid']}) is compulsory, but "
                f"{v['says']}; give it a value in [{_path(p, v).rsplit('.', 1)[0]}]"
            )
    return problems + _invalid_strings(strings, metadata)


def _invalid_strings(strings: list[tuple[dict, dict, Field]], metadata: Metadata) -> list[str]:
    """The problems of the fields the spec gives a string, which is written as it is: one that is
    not a code of the field's option set, or not a valid value of its value type. An org unit's
    or user's depends on the build, which the pre-load checks judge."""
    checked = [
        (p, v, field)
        for p, v, field in strings
        if field.option_set or field.value_type not in ("ORGANISATION_UNIT", "USERNAME")
    ]
    valid = valid_values({(f.value_type, v["gen"]) for _, v, f in checked if not f.option_set})
    problems = []
    for p, v, field in checked:
        if field.option_set:
            codes = {o.code for o in metadata.options(field.option_set)}
            if v["gen"] in codes:
                continue
            why = f"not in its option set, which holds {sorted(codes)}"
        elif (field.value_type, v["gen"]) in valid:
            continue
        else:
            why = f"not a value of type {field.value_type}"
        if expr.references(v["gen"]):
            why += f"; a string is text, an expression {{ sql = {json.dumps(v['gen'])} }}"
        problems.append(f"{_path(p, v)}: {v['gen']!r} is {why}")
    return problems


def _path(p: dict, v: dict) -> str:
    """Where the spec names a field: in its program's values or attributes, by its key."""
    return f"program.{p['key']}.{v['label'].partition('.')[0]}.{v['key']}"


def valid_values(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The (value type, value) pairs whose value is a valid value of the type, as the pre-load
    checks of generate judge it: by valid_value of sql/values.sql."""
    con = duckdb.connect()
    # valid_value reads the build's org units and users, which no value checked is.
    con.execute("create table orgunit (uid varchar, imported_to_dhis2 boolean)")
    con.execute("create table user_ref (username varchar)")
    for statement in sqlfile.script(sqlfile.SQL / "values.sql"):
        con.execute(statement)
    con.execute("create table pair (value_type varchar, code varchar)")
    if pairs:
        con.executemany("insert into pair values (?, ?)", sorted(pairs))
    valid = con.execute("select value_type, code from pair where valid_value(value_type, code)")
    return set(valid.fetchall())


def default_generator(
    field: Field, metadata: Metadata, build: dict, valid_codes: set[tuple[str, str]]
) -> tuple[Any, str]:
    """The generator a field gets from its metadata, and what it says of it. valid_codes: the
    (value type, option code) pairs whose code is a valid value of the type (valid_values)."""
    value_type = field.value_type
    if field.option_set:
        options = metadata.options(field.option_set)
        # The importer checks an option's code against the value type too.
        valid = [o for o in options if (value_type, o.code) in valid_codes]
        if not valid:
            return None, (
                f"empty: no option of option set {field.option_set} is a valid {value_type} value"
            )
        says = f"an option of {len(valid)}, uniform"
        if len(valid) < len(options):
            says += f" (of {len(options)}, the others no valid {value_type} value)"
        return {"weights": {o.code: 1 for o in valid}}, says
    if field.kind == "attribute" and field.pattern:
        return {"pattern": field.pattern}, "its pattern, from the tracked entity's id"
    if field.unique:
        return {"unique": True}, "from the tracked entity's id, so never repeated"
    if value_type in EMPTY_VALUE_TYPES:
        return None, f"empty: {value_type} is not generated"
    window = [date.fromisoformat(build["start"]), date.fromisoformat(build["end"])]
    defaults = {
        "BOOLEAN": ("false", "false, as program rules act on true"),
        "TRUE_ONLY": ("true", "true only"),
        "INTEGER": ({"uniform": [-100, 100], "round": 0}, "uniform in -100 to 100"),
        "INTEGER_POSITIVE": ({"uniform": [1, 100], "round": 0}, "uniform in 1 to 100"),
        "INTEGER_NEGATIVE": ({"uniform": [-100, -1], "round": 0}, "uniform in -100 to -1"),
        "INTEGER_ZERO_OR_POSITIVE": ({"uniform": [0, 100], "round": 0}, "uniform in 0 to 100"),
        "NUMBER": ({"uniform": [0, 100], "round": 1}, "uniform in 0 to 100"),
        "PERCENTAGE": ({"uniform": [0, 100], "round": 0}, "uniform in 0 to 100"),
        "UNIT_INTERVAL": ({"uniform": [0, 1], "round": 2}, "uniform in 0 to 1"),
        "DATE": ({"uniform": window}, "uniform in the data window"),
        "DATETIME": ({"uniform": window}, "uniform in the data window"),
        "TIME": ({"uniform": [8 / 24, 17 / 24]}, "uniform in working hours"),
        "AGE": (
            {"sql": "record.date - least(-25 * 365.25 * ln(1 - u('age')), 100 * 365.25)"},
            "a date of birth, the age exponential of mean 25 years, at most 100",
        ),
        "TEXT": ({"words": {"n": 5000, "s": 1.1}}, "a made-up word, Zipf over 5,000"),
        "LONG_TEXT": ({"words": {"n": 5000, "s": 1.1, "count": 6}}, "6 made-up words"),
        "LETTER": ({"letter": True}, "a letter"),
        "PHONE_NUMBER": ({"phone": True}, "made up after the spare country code +289"),
        "EMAIL": ({"email": True}, "made up at example.org"),
        "URL": ({"url": True}, "made up at example.org"),
        "ORGANISATION_UNIT": ({"orgunit": True}, "the record's org unit"),
        "USERNAME": ({"username": True}, "the record's clerk"),
    }
    if value_type not in defaults:
        return None, f"empty: value type {value_type} unknown to dbgen"
    return defaults[value_type]


def window(effective: dict) -> tuple[date, date, date]:
    """The build's start, end and now."""
    b = effective["build"]
    return (
        date.fromisoformat(b["start"]),
        date.fromisoformat(b["end"]),
        date.fromisoformat(b["now"]),
    )


def summary(gen) -> str:
    """A field's generator in a few words, the says of the effective spec: its distribution and
    parameters."""
    if gen is None:
        return "empty"
    if not isinstance(gen, dict):
        return f"constant {gen!r}"
    kind = next((k for k in gen if k in distribution.DISTRIBUTIONS), next(iter(gen), "?"))
    return f"{kind} {gen[kind]!r}" if kind in gen else kind
