"""Submit tracker payloads to DHIS2's importer to validate, and tally what it reports.

`importMode=VALIDATE` with `importStrategy=CREATE` runs the importer's whole validation of new
records and DHIS2's own rule engine over every program rule, and writes nothing. A tally counts
the errors and warnings by code, program rule and field, which the messages name by UID, with the
records each failed and an example message: the spec field to change is then plain.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import httpx

from dbgen.errors import InternalError

UID_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9]{10}\b")


@dataclass
class Tally:
    """Errors or warnings of the importer by (code, rule, field)."""

    counts: Counter = field(default_factory=Counter)
    examples: dict[tuple, str] = field(default_factory=dict)

    def add(self, code: str, message: str, names: dict[str, tuple[str, str]]) -> None:
        rule = found = None
        for token in UID_TOKEN.findall(message):
            kind = names.get(token, (None, None))[0]
            if kind == "rule" and rule is None:
                rule = token
            elif kind == "field" and found is None:
                found = token
        key = (code, rule, found)
        self.counts[key] += 1
        self.examples.setdefault(key, message)

    def total(self) -> int:
        return sum(self.counts.values())

    def rows(self, names: dict[str, tuple[str, str]]) -> list[dict]:
        """The tally as rows, most frequent first, with the names of the rule and the field."""
        return [
            {
                "code": code,
                "rule": rule,
                "rule_name": names.get(rule, ("", ""))[1] if rule else None,
                "field": found,
                "field_name": names.get(found, ("", ""))[1] if found else None,
                "count": n,
                "example": self.examples[(code, rule, found)],
            }
            for (code, rule, found), n in self.counts.most_common()
        ]


def names(cur) -> dict[str, tuple[str, str]]:
    """Per UID of a program rule, data element or attribute, its kind and its name, by which a
    tally names what a message's UIDs are."""
    cur.execute(
        "select uid, 'rule', name from programrule "
        "union all select uid, 'field', coalesce(code, name) from dataelement "
        "union all select uid, 'field', coalesce(code, name) from trackedentityattribute"
    )
    return {uid: (kind, name) for uid, kind, name in cur.fetchall()}


def validate(api: httpx.Client, payload: dict, errors: Tally, warnings: Tally, known: dict) -> None:
    """Submit a payload with importMode=VALIDATE and add what the importer reports to the
    tallies. A failure other than a validation report (409) raises: it would pass as none."""
    response = api.post(
        "/api/tracker",
        params={
            "importMode": "VALIDATE",
            "importStrategy": "CREATE",
            "async": "false",
            "reportMode": "FULL",
        },
        json=payload,
    )
    if response.status_code != 409 and response.is_error:
        raise InternalError(f"the importer answered {response.status_code}: {response.text[:2000]}")
    report = response.json().get("validationReport", {})
    for warning in report.get("warningReports", []):
        warnings.add(warning.get("warningCode", ""), warning.get("message", ""), known)
    for error in report.get("errorReports", []):
        errors.add(error.get("errorCode", ""), error.get("message", ""), known)


def summary(title: str, errors: Tally, warnings: Tally, known: dict, limit: int = 25) -> str:
    """Lines saying what the tallies hold, the most frequent first."""
    lines = [f"{title}: {errors.total()} errors, {warnings.total()} warnings"]
    for kind, tally in (("error", errors), ("warning", warnings)):
        for row in tally.rows(known)[:limit]:
            rule = f" rule {row['rule']} {row['rule_name']!r}" if row["rule"] else ""
            fld = f" field {row['field']} {row['field_name']}" if row["field"] else ""
            lines.append(f"  {row['count']:7d} {kind} {row['code']}{rule}{fld}")
            if kind == "error":
                lines.append(f"          {row['example'][:200]}")
    return "\n".join(lines)


def unvalidated(effective: dict) -> set[str]:
    """The UIDs of the fields the spec leaves out of what is submitted (its unvalidated)."""
    return {uid for uids in effective.get("unvalidated", {}).values() for uid in uids}


def without(payload: dict, fields: set[str]) -> dict:
    """The payload with no value of the fields: the attributes of its tracked entities and
    enrollments, and the data values of their events. Changes and returns the payload."""
    if not fields:
        return payload
    for te in payload.get("trackedEntities", []):
        for owner in [te, *te.get("enrollments", [])]:
            if "attributes" in owner:
                owner["attributes"] = [
                    a for a in owner["attributes"] if a["attribute"] not in fields
                ]
            for event in owner.get("events", []):
                event["dataValues"] = [
                    v for v in event.get("dataValues", []) if v["dataElement"] not in fields
                ]
    return payload
