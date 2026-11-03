"""`dbgen explain`: what a spec builds, without building it.

Prints the effective spec: every program and stage, each data element and attribute that gets a
value with its generator and where that came from (default or spec), how many optional fields
each stage leaves empty, and the records it expects, from the org units the spec draws and the
means of its distributions. These come out high: a mean leaves out a bound that is an expression,
such as a visit count's max, the records the window's end cuts, and the source enrollments a
linked program's where leaves out. Fails, as compile does, on a name, option or expression the
build would fail on. The program rules are the spec's to satisfy: `dbgen validate` checks them.
"""

import json
import re
import sys

import duckdb

from dbgen import compile as compile_stage
from dbgen import distribution, generate
from dbgen.config import Build, shown
from dbgen.errors import UsageError
from dbgen.generate import Program, prepare
from dbgen.orgunits import build_orgunits

# What the line of a linked program's from leaves out: the attributes, which their lines show,
# and what compile derives, which the spec does not write.
NOT_SHOWN_FROM = ("attributes", "relationship_from_source", "relationship_type")

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def run(build: Build, program: str | None = None, as_json: bool = False) -> None:
    effective, _ = compile_stage.compile_build(build)
    # Fail as compile does on an expression it would reject, so explain checks a whole spec.
    generate.check_binds(effective, generate.statements(effective))
    if as_json:
        print(json.dumps(effective, indent=1, default=str))
        return
    programs = effective["programs"]
    if program:
        programs = [
            p
            for p in programs
            if program.lower() in {n.lower() for n in (p["key"], p["uid"], p["name"])}
        ]
        if not programs:
            raise UsageError(
                f"--program {program}: no such program in the spec; its programs are "
                + ", ".join(p["key"] for p in effective["programs"])
            )
    out = sys.stdout
    b = effective["build"]
    out.write(f"spec {shown(build.spec)}, build {build.name}\n")
    out.write(f"  metadata, in import order: {', '.join(m['file'] for m in b['metadata'])}\n")
    out.write(f"  seed {b['seed']}, window {b['start']} to {b['end']}, now {b['now']}\n")
    con = build_orgunits(effective)
    levels = con.execute(
        "select level, level_name, count(*) filter (kept), count(*) from orgunit "
        "group by all order by level"
    ).fetchall()
    out.write("\norg units in the build, of those the spec draws:\n")
    for level, name, kept, total in levels:
        out.write(f"  {name} (level {level}): {kept:,} of {total:,}\n")
    expected = _expected(con, effective)
    for p in programs:
        e = expected[p["key"]]
        out.write(f"\nprogram {p['key']}: {p['name']} ({p['uid']})\n")
        out.write(f"  registers at {p['orgunit_level']}, access level {p['access_level']}\n")
        out.write(
            f"  expects about {e['tracked_entities']:,.0f} tracked entities, "
            f"{e['enrollments']:,.0f} enrollments, {e['events']:,.0f} events\n"
        )
        if p["visits"]:
            out.write(
                f"  visits: count {toml(p['visits']['count'])}, "
                f"spacing {toml(p['visits']['spacing'])} days\n"
            )
        if p["from"]:
            linked = {k: v for k, v in p["from"].items() if k not in NOT_SHOWN_FROM}
            out.write(
                f"  linked from the enrollments of {linked.pop('enrollments')}: {toml(linked)}\n"
            )
        for s in p["stages"]:
            how = (
                "in the visits"
                if s["in_visits"]
                else (
                    f"repeats {toml(s['count'])}, every {toml(s['spacing'])} days"
                    if s["repeatable"]
                    else f"once, at {s['at']}"
                    + ("" if s["offset"] == 0 else f" + {toml(s['offset'])} days")
                    + f", share {toml(s['share'])}"
                )
            )
            out.write(f"  stage {s['name']} ({s['uid']}): {how}, status {toml(s['status'])}\n")
            _fields(out, s["values"])
        out.write("  attributes:\n")
        _fields(out, p["attributes"], _copies(effective, p))
        skipped = effective["unvalidated"].get(p["key"], [])
        if skipped:
            names = {
                f["uid"]: f["code"] or f["name"]
                for f in [*p["attributes"], *(v for s in p["stages"] for v in s["values"])]
            }
            out.write(
                "  unvalidated, left out of what validate and verify submit: "
                + ", ".join(f"{uid} ({names.get(uid, '?')})" for uid in skipped)
                + "\n"
            )


def toml(value) -> str:
    """A value of the effective spec as the spec writes it, in TOML, such as
    `{ poisson = 4.0, plus = 1 }`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        if not value:
            return "{}"
        pairs = (
            f"{k if BARE_KEY.fullmatch(str(k)) else json.dumps(str(k))} = {toml(v)}"
            for k, v in value.items()
        )
        return "{ " + ", ".join(pairs) + " }"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(re.sub(r"\n\s*", " ", value))  # one line
    return str(value)


def _copies(effective: dict, p: dict) -> dict[str, str]:
    """What each attribute a linked tracked entity copies is a copy of, by UID, such as
    mother.GIVEN_NAME: the source's attribute by its code, or UID if it has none."""
    if not p["from"]:
        return {}
    source = next(q for q in effective["programs"] if q["key"] == p["from"]["enrollments"])
    names = {a["uid"]: a["code"] or a["uid"] for a in source["attributes"]}
    return {
        uid: f"{p['from']['as']}.{names[copied['from']]}"
        for uid, copied in p["from"]["attributes"].items()
    }


def _fields(out, fields: list[dict], copies: dict[str, str] | None = None) -> None:
    """The fields that get a value, then how many optional ones the spec leaves empty."""
    copies = copies or {}
    empty = 0
    for v in fields:
        if v["source"] == "default" and v["gen"] is None and v["uid"] not in copies:
            empty += 1
        else:
            _field(out, v, copies.get(v["uid"]))
    if empty:
        out.write(
            f"    {empty} optional field{'s' if empty != 1 else ''} left empty, as the spec sets "
            "no value\n"
        )


def _field(out, v: dict, copy: str | None) -> None:
    flags = []
    if v["required"]:
        flags.append("compulsory")
    if v.get("unique"):
        flags.append("unique")
    # The spec's entry as the spec writes it; a default's says why the metadata gives it.
    says = v["says"]
    if v["source"] == "spec":
        entry = v["gen"]
        extra = ({"if": v["if"]} if v.get("if") else {}) | (
            {"fill": v["fill"]} if v["fill"] < 1 else {}
        )
        if extra:
            entry = (entry if isinstance(entry, dict) else {"constant": entry}) | extra
        says = toml(entry)
    source = v["source"]
    if copy:
        # A linked tracked entity copies its source's; the others draw the field's generator.
        says = f"{copy} if linked" + ("" if v["gen"] is None else f", else {says}")
        source = "spec"
    # The key the spec names the field by, its code or UID for a default, and its name unless
    # that is the key.
    key = v["key"]
    shown = (key if BARE_KEY.fullmatch(key) else json.dumps(key)) + (
        "" if v["name"] == key else f" {json.dumps(v['name'])}"
    )
    out.write(
        f"    {shown} ({v['value_type']}{', ' if flags else ''}"
        f"{', '.join(flags)}): {says} [{source}]\n"
    )


def _expected(con: duckdb.DuckDBPyConnection, effective: dict) -> dict[str, dict]:
    """The records each program expects in the build's area."""
    none = {"tracked_entities": 0.0, "enrollments": 0.0, "events": 0.0}
    return {
        key: areas.get("build", none)
        for key, areas in expected(con, effective, "case when o.kept then 'build' end").items()
    }


def expected(
    con: duckdb.DuckDBPyConnection, effective: dict, area: str
) -> dict[str, dict[str, dict]]:
    """The records each program expects per area, from the means of its distributions: area is a
    SQL expression of the area of org unit o, a place of the program, null for none. con holds
    table orgunit (orgunits.build_orgunits)."""
    result = {}
    for p in effective["programs"]:
        prog = Program(p, effective)
        for statement in prog.place():
            con.execute(statement)
        prepare(con, effective)
        mean_counts = dict(
            con.execute(
                f"""
                select {area} as area, sum(m.mean)
                from ({prog.mean_tracked_entities()}) m
                join orgunit o on o.code = m.orgunit_code
                where area is not null
                group by all
                """
            ).fetchall()
        )
        if p["from"] is not None:
            # At most: where and the window leave some out. A linked tracked entity registers at
            # its source's org unit, so in its area.
            share = p["from"]["share"]
            for a, source in result.get(p["from"]["enrollments"], {}).items():
                mean_counts[a] = mean_counts.get(a, 0.0) + source["enrollments"] * share
        enrollments_per = max(1.0, distribution.mean_of(p["enrollments"]) or 1.0)
        events_per = 0.0
        visits = distribution.mean_of(p["visits"]["count"]) if p["visits"] else None
        for s in p["stages"]:
            if s["in_visits"]:
                n = visits or 0.0
            elif s["repeatable"]:
                n = s["share"] * (distribution.mean_of(s["count"]) or 1.0)
            else:
                n = s["share"]
            events_per += n
        result[p["key"]] = {}
        for a, mean_count in mean_counts.items():
            enrollments = mean_count * enrollments_per
            events = enrollments * events_per
            result[p["key"]][a] = {
                "tracked_entities": mean_count,
                "enrollments": enrollments,
                "events": events,
            }
    return result
