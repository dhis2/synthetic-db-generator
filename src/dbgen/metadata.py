"""The metadata a build imports, read from its JSON files: what the data must look like.

The files are DHIS2 metadata exports, imported in the order the spec lists them (bootstrap). An
object of a later file replaces the object of the same type and UID of an earlier one whole, as
the import does: when two files ship one tracked entity type, DHIS2 keeps the one imported last,
with its attributes and sharing only.

The generator reads only what decides whether data is valid, and how it is shaped by default:

* tracker programs, their tracked entity type, attributes, stages and category combo
* the data elements of each stage, compulsory or not, and their value types and option sets
* tracked entity attributes: value type, option set, unique, generated pattern
* relationship types
* the user groups that the programs and tracked entity types are shared with, and the access
  each has, which decides who captures and who views (users.py)
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dbgen.errors import UsageError, input_file


@dataclass(frozen=True)
class Option:
    uid: str
    code: str
    name: str


@dataclass(frozen=True)
class Field:
    """A data element or a tracked entity attribute: what a value is drawn for."""

    uid: str
    code: str | None
    name: str
    value_type: str
    option_set: str | None  # its UID
    unique: bool = False
    pattern: str | None = None  # the TextPattern of a generated attribute
    kind: str = "dataElement"  # or "attribute"


@dataclass(frozen=True)
class StageField:
    uid: str  # of the data element
    compulsory: bool


@dataclass(frozen=True)
class Stage:
    uid: str
    name: str
    program: str
    repeatable: bool
    sort_order: int
    standard_interval: int | None
    min_days_from_start: int
    fields: tuple[StageField, ...]


@dataclass(frozen=True)
class ProgramAttribute:
    uid: str  # of the attribute
    mandatory: bool
    searchable: bool


@dataclass(frozen=True)
class Program:
    uid: str
    code: str | None
    name: str
    short_name: str
    tracked_entity_type: str
    category_combo: str | None
    access_level: str
    only_enroll_once: bool
    stages: tuple[Stage, ...]
    attributes: tuple[ProgramAttribute, ...]
    sharing: dict


@dataclass
class Metadata:
    """The metadata of the files, in their import order."""

    files: list[Path]
    sha256: dict[str, str]
    objects: dict[str, dict[str, dict]] = field(default_factory=dict)  # type -> uid -> object

    def of(self, kind: str) -> list[dict]:
        return list(self.objects.get(kind, {}).values())

    def get(self, kind: str, uid: str) -> dict | None:
        return self.objects.get(kind, {}).get(uid)

    @property
    def programs(self) -> list[Program]:
        """The tracker programs, in import order, not the event programs (event_programs)."""
        return [
            self._program(p)
            for p in self.of("programs")
            if p.get("programType", "WITH_REGISTRATION") == "WITH_REGISTRATION"
        ]

    @property
    def event_programs(self) -> list[dict]:
        """The programs without registration, whose events dbgen does not generate."""
        return [p for p in self.of("programs") if p.get("programType") == "WITHOUT_REGISTRATION"]

    def _program(self, p: dict) -> Program:
        stages = []
        for ref in p.get("programStages", []):
            stage = _need(ref, "id", "a stage of program", p["id"])
            s = self.get("programStages", stage)
            if s is None:
                raise UsageError(
                    f"program {p['id']} references stage {stage}, which the metadata files "
                    "do not hold"
                )
            stages.append(
                Stage(
                    uid=s["id"],
                    name=_need(s, "name", "program stage", s["id"]).strip(),
                    program=p["id"],
                    repeatable=bool(s.get("repeatable", False)),
                    sort_order=int(s.get("sortOrder") or 0),
                    standard_interval=s.get("standardInterval"),
                    min_days_from_start=int(s.get("minDaysFromStart") or 0),
                    fields=tuple(
                        StageField(
                            uid=_need(psde, "dataElement.id", "a field of stage", s["id"]),
                            compulsory=bool(psde.get("compulsory", False)),
                        )
                        for psde in s.get("programStageDataElements", [])
                    ),
                )
            )
        stages.sort(key=lambda s: (s.sort_order, s.name))
        name = _need(p, "name", "program", p["id"])
        return Program(
            uid=p["id"],
            code=p.get("code"),
            name=name,
            short_name=p.get("shortName", name),
            tracked_entity_type=_need(p, "trackedEntityType.id", "program", p["id"]),
            category_combo=(p.get("categoryCombo") or {}).get("id"),
            access_level=p.get("accessLevel", "OPEN"),
            only_enroll_once=bool(p.get("onlyEnrollOnce", False)),
            stages=tuple(stages),
            attributes=tuple(
                ProgramAttribute(
                    uid=_need(a, "trackedEntityAttribute.id", "an attribute of program", p["id"]),
                    mandatory=bool(a.get("mandatory", False)),
                    searchable=bool(a.get("searchable", False)),
                )
                for a in p.get("programTrackedEntityAttributes", [])
            ),
            sharing=p.get("sharing") or {},
        )

    def data_element(self, uid: str) -> Field:
        de = self.get("dataElements", uid)
        if de is None:
            raise UsageError(f"data element {uid} is used, but the metadata files do not hold it")
        return Field(
            uid=uid,
            code=de.get("code"),
            name=_need(de, "name", "data element", uid),
            value_type=_need(de, "valueType", "data element", uid),
            option_set=(de.get("optionSet") or {}).get("id"),
        )

    def attribute(self, uid: str) -> Field:
        tea = self.get("trackedEntityAttributes", uid)
        if tea is None:
            raise UsageError(f"attribute {uid} is used, but the metadata files do not hold it")
        return Field(
            uid=uid,
            code=tea.get("code"),
            name=_need(tea, "name", "attribute", uid),
            value_type=_need(tea, "valueType", "attribute", uid),
            option_set=(tea.get("optionSet") or {}).get("id"),
            unique=bool(tea.get("unique", False)),
            pattern=tea.get("pattern") or None if tea.get("generated") else None,
            kind="attribute",
        )

    def options(self, option_set: str) -> list[Option]:
        """The options of an option set, in their order."""
        os_ = self.get("optionSets", option_set)
        if os_ is None:
            raise UsageError(
                f"option set {option_set} is used, but the metadata files do not hold it"
            )
        options = []
        for ref in os_.get("options", []):
            option = _need(ref, "id", "an option of option set", option_set)
            o = self.get("options", option)
            if o is None:
                raise UsageError(
                    f"option set {option_set} references option {option}, which the "
                    "metadata files do not hold"
                )
            code = _need(o, "code", "option", option)
            name = _need(o, "name", "option", option)
            options.append((o.get("sortOrder", 0), Option(uid=option, code=code, name=name)))
        return [o for _, o in sorted(options, key=lambda x: x[0])]

    def type_attributes(self, tracked_entity_type: str) -> list[str]:
        tet = self.get("trackedEntityTypes", tracked_entity_type) or {}
        return [
            _need(a, "trackedEntityAttribute.id", "an attribute of type", tracked_entity_type)
            for a in tet.get("trackedEntityTypeAttributes", [])
        ]

    def category_option_combos(self, category_combo: str | None) -> list[dict]:
        """The option combos of a category combo, or none when the metadata does not ship it:
        then DHIS2's default."""
        if category_combo is None:
            return []
        return sorted(
            (
                coc
                for coc in self.of("categoryOptionCombos")
                if (coc.get("categoryCombo") or {}).get("id") == category_combo
            ),
            key=lambda c: c["id"],
        )

    @property
    def relationship_types(self) -> list[dict]:
        return self.of("relationshipTypes")

    def user_group(self, uid: str) -> dict | None:
        return self.get("userGroups", uid)


def _need(obj: dict, path: str, kind: str, uid: str):
    """The value at the dotted path of a metadata object. Fails naming the object, of kind and
    uid, when the path is missing."""
    value = obj
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise UsageError(f"{kind} {uid} in the metadata files has no {path}")
        value = value[key]
    return value


def read(files: Iterable[Path]) -> Metadata:
    """The metadata of the files, each object of a later file replacing the same of an earlier."""
    files = list(files)
    metadata = Metadata(files=files, sha256={})
    for path in files:
        data = input_file(path, "metadata file").read_bytes()
        metadata.sha256[str(path)] = hashlib.sha256(data).hexdigest()
        try:
            payload = json.loads(data)
        except ValueError as e:
            raise UsageError(f"metadata file {path} is not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise UsageError(f"metadata file {path} is not a metadata export (an object)")
        for kind, objects in payload.items():
            if not isinstance(objects, list):
                continue
            of_kind = metadata.objects.setdefault(kind, {})
            for obj in objects:
                if isinstance(obj, dict) and "id" in obj:
                    of_kind[obj["id"]] = obj
    return metadata


# The columns of each of the tables, which hold them also without rows.
SCHEMAS = {
    "program": (
        "uid varchar, trackedentitytype_uid varchar, only_enroll_once boolean, "
        "orgunit_level varchar"
    ),
    "programstage": "uid varchar, program_uid varchar, repeatable boolean",
    "programstagedataelement": (
        "programstage_uid varchar, dataelement_uid varchar, compulsory boolean"
    ),
    "dataelement": "uid varchar, valuetype varchar, optionset_uid varchar",
    "option": "optionset_uid varchar, code varchar",
    "trackedentityattribute": (
        'uid varchar, valuetype varchar, optionset_uid varchar, "unique" boolean'
    ),
    "program_attribute": (
        "program_uid varchar, trackedentityattribute_uid varchar, mandatory boolean"
    ),
    "trackedentitytypeattribute": (
        "trackedentitytype_uid varchar, trackedentityattribute_uid varchar"
    ),
    "relationshiptype": "uid varchar",
}


def tables(metadata: Metadata, effective: dict) -> dict[str, list[dict]]:
    """What the pre-load checks read of the metadata, as tables by name: the programs of the
    effective spec, their stages and data elements, the option codes, the attributes of the
    programs and of their tracked entity types, and the relationship types."""
    out: dict[str, list[dict]] = {
        "program": [],
        "programstage": [],
        "programstagedataelement": [],
        "dataelement": [],
        "option": [],
        "trackedentityattribute": [],
        "program_attribute": [],
        "trackedentitytypeattribute": [],
        "relationshiptype": [],
    }
    option_sets: set[str] = set()
    data_elements: dict[str, Field] = {}
    attributes: dict[str, Field] = {}
    by_uid = {p.uid: p for p in metadata.programs}
    for e in effective["programs"]:
        p = by_uid[e["uid"]]
        out["program"].append(
            {
                "uid": p.uid,
                "trackedentitytype_uid": p.tracked_entity_type,
                "only_enroll_once": p.only_enroll_once,
                "orgunit_level": e["orgunit_level"],
            }
        )
        for s in p.stages:
            out["programstage"].append(
                {"uid": s.uid, "program_uid": p.uid, "repeatable": s.repeatable}
            )
            for f in s.fields:
                out["programstagedataelement"].append(
                    {
                        "programstage_uid": s.uid,
                        "dataelement_uid": f.uid,
                        "compulsory": f.compulsory,
                    }
                )
                data_elements[f.uid] = metadata.data_element(f.uid)
        for a in p.attributes:
            out["program_attribute"].append(
                {
                    "program_uid": p.uid,
                    "trackedentityattribute_uid": a.uid,
                    "mandatory": a.mandatory,
                }
            )
            attributes[a.uid] = metadata.attribute(a.uid)
        for uid in metadata.type_attributes(p.tracked_entity_type):
            out["trackedentitytypeattribute"].append(
                {"trackedentitytype_uid": p.tracked_entity_type, "trackedentityattribute_uid": uid}
            )
            attributes[uid] = metadata.attribute(uid)
    for f in data_elements.values():
        out["dataelement"].append(
            {"uid": f.uid, "valuetype": f.value_type, "optionset_uid": f.option_set}
        )
        if f.option_set:
            option_sets.add(f.option_set)
    for f in attributes.values():
        out["trackedentityattribute"].append(
            {
                "uid": f.uid,
                "valuetype": f.value_type,
                "optionset_uid": f.option_set,
                "unique": f.unique,
            }
        )
        if f.option_set:
            option_sets.add(f.option_set)
    for os_ in sorted(option_sets):
        for o in metadata.options(os_):
            out["option"].append({"optionset_uid": os_, "code": o.code})
    for rt in metadata.relationship_types:
        out["relationshiptype"].append({"uid": rt["id"]})
    return out
