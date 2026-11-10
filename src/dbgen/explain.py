"""`dbgen explain`: what a spec builds, without building it.

Prints the effective spec as an overlay of the metadata, block by block as the spec writes it:
every key the spec sets on a line of its own, then on one line what the metadata gives the others,
naming its property, and on one more dbgen's defaults. A key the spec sets over the metadata names
what it replaces, such as [over standardInterval 30]. What does not happen, such as a share of 0,
is left out. Each data element and attribute that gets a value comes with its generator, marked
[default] where the spec leaves it to the metadata's, and each stage says how many optional fields
it leaves empty.

Prints too the records each program expects, from the org units the spec draws and the means of
its distributions. These come out high: a mean leaves out a bound that is an expression, such as a
visit count's max, the records the window's end cuts, and the source enrollments a linked
program's where leaves out. Fails, as compile does, on a name, option or expression the build
would fail on. The program rules are the spec's to satisfy: `dbgen validate` checks them.
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
from dbgen.metadata import Stage
from dbgen.orgunits import build_orgunits

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
# The width a line keeps to: a value that does not fit after its key goes on a line of its own.
WIDTH = 100


def run(build: Build, program: str | None = None, as_json: bool = False) -> None:
    effective, metadata = compile_stage.compile_build(build)
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
    stages = {s.uid: s for p in metadata.programs for s in p.stages}
    out = Out()
    given = effective["spec_keys"]
    b = effective["build"]
    out.line(0, f"spec {shown(build.spec)}, build {build.name}")
    block = Block(given["build"])
    block.key("metadata", [m["file"] for m in b["metadata"]])
    block.key("seed", b["seed"])
    for name in ("start", "end", "now"):
        block.key(name, b[name], text=b[name])  # a TOML date, unquoted
    block.write(out, 2)
    out.line(0, "users:")
    block = Block(given["users"])
    for name in ("prefix", "password", "clerks", "officers", "search_level"):
        block.key(name, effective["users"][name])
    block.write(out, 2)
    if effective["records"]["updated"]["share"] > 0:
        out.line(0, "records:")
        block = Block(given["records"])
        block.key("updated", effective["records"]["updated"])
        block.write(out, 2)
    out.line(0, "load:")
    block = Block(given["load"])
    block.key("order", effective["load"]["order"])
    block.write(out, 2)
    con = build_orgunits(effective)
    levels = con.execute(
        "select level, level_name, count(*) filter (imported_to_dhis2), count(*) from orgunit "
        "group by all order by level"
    ).fetchall()
    out.line(0, "")
    out.line(0, "org units in the build, of those the spec draws:")
    for level, name, imported, total in levels:
        out.line(2, f"{name} (level {level}): {imported:,} of {total:,}")
    expected = _expected(con, effective)
    for p in programs:
        out.line(0, "")
        _program(out, effective, p, given["programs"][p["key"]], stages, expected[p["key"]])


class Out:
    """Lines to stdout, each kept to WIDTH where it can be."""

    def line(self, indent: int, head: str, value: str | None = None, tag: str | None = None):
        """A line of head and value, the value on a line of its own below when both do not fit,
        and the tag, if any, after it in brackets."""
        tail = ("" if value is None else f" {value}") + (f" [{tag}]" if tag else "")
        pad = " " * indent
        if value is None or len(pad + head + tail) <= WIDTH:
            sys.stdout.write(f"{pad}{head}{tail}".rstrip() + "\n")
        else:
            sys.stdout.write(f"{pad}{head}\n{pad}    {tail.lstrip()}\n")

    def items(self, indent: int, head: str, items: list[str]) -> None:
        """head and the items, as many to a line as fit, the lines after the first indented."""
        lines = [" " * indent + head]
        for item in items:
            if len(f"{lines[-1]} {item},") > WIDTH and lines[-1].strip() != head:
                lines.append(" " * (indent + 4) + item)
            else:
                lines[-1] += f" {item}"
            lines[-1] += ","
        lines[-1] = lines[-1].removesuffix(",")
        sys.stdout.write("\n".join(lines) + "\n")


class Block:
    """The keys of a table of the spec: those the spec sets (given) each on a line, then what the
    metadata gives the others on one, and dbgen's defaults on another."""

    def __init__(self, given: list[str]):
        self.given = set(given)
        self.keys: list[tuple[str, str, str | None]] = []  # name, value, where it comes from

    def key(
        self,
        name: str,
        value,
        unit: str = "",
        metadata: tuple[str, object] | None = None,
        text: str | None = None,
    ):
        """A key and its value, shown as text if given, else as TOML. metadata: the property of
        the metadata that gives the key's default, and its value there, None where it has none."""
        text = (toml(value) if text is None else text) + unit
        if name in self.given:
            if metadata and metadata[1] is not None and metadata[1] != value:
                self.keys.append((name, text, f"over {metadata[0]} {toml(metadata[1])}"))
            else:
                self.keys.append((name, text, None))
        elif metadata and metadata[1] is not None:
            self.keys.append((name, text, metadata[0]))
        else:
            self.keys.append((name, text, "default"))

    def write(self, out: Out, indent: int) -> None:
        by_metadata, by_default = [], []
        for name, text, source in self.keys:
            if source is None or source.startswith("over "):
                out.line(indent, name, text, source)
            elif source == "default":
                by_default.append(f"{name} {text}")
            else:
                by_metadata.append(f"{name} {text} ({source})")
        if by_metadata:
            out.items(indent, "metadata:", by_metadata)
        if by_default:
            out.items(indent, "default:", by_default)


def _program(
    out: Out, effective: dict, p: dict, given: dict, stages: dict[str, Stage], e: dict
) -> None:
    out.line(0, f"program {p['key']}: {p['name']} ({p['uid']}), access level {p['access_level']}")
    block = Block(given["program"])
    block.key("orgunit_level", p["orgunit_level"])
    block.key("tracked_entities", {k: v for k, v in p["tracked_entities"].items() if v is not None})
    block.write(out, 2)
    out.line(
        2,
        f"expects about {e['tracked_entities']:,.0f} tracked entities, "
        f"{e['enrollments']:,.0f} enrollments, {e['events']:,.0f} events",
    )
    if p["from"]:
        f = p["from"]
        out.line(2, "from:")
        block = Block(given["from"])
        block.key("enrollments", {"of": f["enrollments"], "as": f["as"]})
        for name in ("where", "share", "relationship", "at"):
            if f[name] is not None:
                block.key(name, f[name])
        block.write(out, 4)
    out.line(2, "enrollments:")
    block = Block(given["enrollments"])
    block.key("count", p["enrollments"])
    block.key("gap", p["gap"], " days")
    block.key("status", p["status"])
    if p["followup"] > 0:
        block.key("followup", p["followup"])
    block.write(out, 4)
    if p["visits"]:
        v = p["visits"]
        names = {s["uid"]: s["name"] for s in p["stages"]}
        out.line(2, "visits:")
        block = Block(given["visits"])
        block.key("stages", [names[uid] for uid in v["stages"]])
        block.key("count", v["count"])
        block.key(
            "spacing",
            v["spacing"],
            " days",
            ("standardInterval", stages[v["stages"][0]].standard_interval),
        )
        block.key("status", v["status"])
        block.write(out, 4)
    for s in p["stages"]:
        _stage(out, s, given["stages"].get(s["uid"], []), stages[s["uid"]])
    out.line(2, "attributes:")
    _fields(out, p["attributes"], _copies(effective, p))
    skipped = effective["unvalidated"].get(p["key"], [])
    if skipped:
        names = {
            f["uid"]: f["code"] or f["name"]
            for f in [*p["attributes"], *(v for s in p["stages"] for v in s["values"])]
        }
        out.line(
            2,
            "unvalidated, left out of what validate and verify submit:",
            ", ".join(f"{uid} ({names.get(uid, '?')})" for uid in skipped),
        )


def _stage(out: Out, s: dict, given: list[str], stage: Stage) -> None:
    how = "in the visits" if s["in_visits"] else "repeatable" if s["repeatable"] else "once"
    out.line(2, f"stage {s['name']} ({s['uid']}), {how}:")
    block = Block(given)
    if not s["in_visits"]:
        block.key("at", s["at"])
        # 0, DHIS2's default, is dbgen's too, so the metadata gives an offset only above it.
        block.key(
            "offset", s["offset"], " days", ("minDaysFromStart", stage.min_days_from_start or None)
        )
        block.key("share", s["share"])
        if s["repeatable"]:
            block.key("count", s["count"])
            block.key(
                "spacing", s["spacing"], " days", ("standardInterval", stage.standard_interval)
            )
    # A stage of the visits takes the visits' status, unless it sets its own.
    if not s["in_visits"] or "status" in given:
        block.key("status", s["status"])
    if s["assigned"] > 0:
        block.key("assigned", s["assigned"])
    block.write(out, 4)
    if s["values"]:
        out.line(4, "values:")
        _fields(out, s["values"], indent=6)


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


def _fields(out: Out, fields: list[dict], copies: dict | None = None, indent: int = 4) -> None:
    """The fields that get a value, then how many optional ones the spec leaves empty."""
    copies = copies or {}
    empty = 0
    for v in fields:
        if v["source"] == "default" and v["gen"] is None and v["uid"] not in copies:
            empty += 1
        else:
            _field(out, v, copies.get(v["uid"]), indent)
    if empty:
        out.line(
            indent,
            f"{empty} optional field{'s' if empty != 1 else ''} left empty, as the spec sets no "
            "value",
        )


def _field(out: Out, v: dict, copy: str | None, indent: int) -> None:
    flags = []
    if v["required"]:
        flags.append("compulsory")
    if v.get("unique"):
        flags.append("unique")
    # The spec's entry as the spec writes it; a default's says why the metadata gives it.
    says = v["says"]
    tag = "default" if v["source"] == "default" else None
    if v["source"] == "spec":
        entry = v["gen"]
        extra = ({"if": v["if"]} if v.get("if") else {}) | (
            {"fill": v["fill"]} if v["fill"] < 1 else {}
        )
        if extra:
            entry = (entry if isinstance(entry, dict) else {"constant": entry}) | extra
        says = toml(entry)
    if copy:
        # A linked tracked entity copies its source's; the others draw the field's generator.
        says = f"{copy} if linked" + ("" if v["gen"] is None else f", else {says}")
        tag = None
    # The key the spec names the field by, its code or UID for a default, and its name unless
    # that is the key.
    key = v["key"]
    head = (key if BARE_KEY.fullmatch(key) else json.dumps(key)) + (
        "" if v["name"] == key else f" {json.dumps(v['name'])}"
    )
    out.line(
        indent, f"{head} ({v['value_type']}{', ' if flags else ''}{', '.join(flags)}):", says, tag
    )


def _expected(con: duckdb.DuckDBPyConnection, effective: dict) -> dict[str, dict]:
    """The records each program expects in the build's area."""
    none = {"tracked_entities": 0.0, "enrollments": 0.0, "events": 0.0}
    return {
        key: areas.get("build", none)
        for key, areas in expected(
            con, effective, "case when o.draws_records_and_users then 'build' end"
        ).items()
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
