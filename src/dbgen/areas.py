"""`dbgen areas`: the org units a build can cover, and the records a build of each would hold.

Lists an org unit, the root by default, and the org units of the level below it: each with its
code, name, the number of its children, its type and traits, and the tracked entities and events
a build of it would hold. These are what `dbgen explain` expects, from the org units the spec
draws and the means of its distributions: an estimate, which comes out high, not a count. The
spec sets causes, such as a district's people, and this shows what they come to, to choose the
area of a build.

It draws the whole hierarchy, as every build does, and writes nothing.
"""

import json
import sys

from dbgen import compile as compile_stage
from dbgen.config import Build, shown
from dbgen.distribution import quote
from dbgen.errors import UsageError
from dbgen.explain import expected
from dbgen.orgunits import build_orgunits


def run(build: Build, code: str | None = None) -> None:
    rows = areas(build, code)
    if code:
        example = rows[0]["code"]
    else:
        # The root is all of the spec's org units: suggest its smallest child that holds records.
        numbers = sorted(rows[1:], key=lambda r: r["events"])
        example = next((r["code"] for r in numbers if r["events"] >= 1), rows[0]["code"])
    _print(rows)
    print(
        "\nThe tracked entities and events are estimates from the spec's means: a build holds "
        "fewer."
    )
    print(f"To build one: dbgen build {shown(build.spec)} --orgunit {example}")


def areas(build: Build, code: str | None = None) -> list[dict]:
    """The org unit of the code, or the root, then its children: each with its code, name,
    level, type, children, traits, and the tracked entities and events expected."""
    effective, _ = compile_stage.compile_build(build)
    con = build_orgunits(effective)
    code = code or effective["orgunits"]["root"]["code"]
    found = con.execute("select level, path from orgunit where code = ?", [code]).fetchone()
    if found is None:
        raise UsageError(
            f"spec {shown(build.spec)} draws no org unit {code}; dbgen areas "
            f"{shown(build.spec)} lists its org units"
        )
    level, path = found
    below = f"starts_with(o.path, {quote(path + '/')})"
    itself = f"case when o.path = {quote(path)} or {below} then {quote(code)} end"
    child = f"case when {below} then split_part(o.path, '/', {level + 1}) end"
    totals, children = (_records(con, effective, area) for area in (itself, child))
    rows = con.execute(
        """
        select o.code, o.name, o.level_name, o.type, o.traits,
               (select count(*) from orgunit c where c.parent_code = o.code) as children
        from orgunit o
        where o.code = ? or o.parent_code = ?
        order by o.level, o.code
        """,
        [code, code],
    ).fetchall()
    result = []
    for c, name, level_name, type_, traits, n in rows:
        records = (totals if c == code else children).get(c, {})
        result.append(
            {
                "code": c,
                "name": name,
                "level": level_name,
                "type": type_,
                "children": n,
                # weight is the type's, which the type says.
                "traits": {k: v for k, v in json.loads(traits).items() if k != "weight"},
                "tracked_entities": sum(r["tracked_entities"] for r in records.values()),
                "events": sum(r["events"] for r in records.values()),
            }
        )
    return result


def _records(con, effective: dict, area: str) -> dict[str, dict[str, dict]]:
    """The records of each program per area, by area."""
    by_area: dict[str, dict[str, dict]] = {}
    for key, per_area in expected(con, effective, area).items():
        for a, records in per_area.items():
            by_area.setdefault(a, {})[key] = records
    return by_area


def _print(rows: list[dict]) -> None:
    traits = list(dict.fromkeys(t for r in rows for t in r["traits"]))
    types = any(r["type"] for r in rows)
    header = [
        "code",
        "level",
        "name",
        *(["type"] if types else []),
        "children",
        *traits,
        "tracked entities",
        "events",
    ]
    table = [header]
    for r in rows:
        table.append(
            [
                r["code"],
                r["level"],
                r["name"],
                *([r["type"] or ""] if types else []),
                f"{r['children']:,}",
                *(_number(r["traits"].get(t)) for t in traits),
                f"{r['tracked_entities']:,.0f}",
                f"{r['events']:,.0f}",
            ]
        )
    # Text to the left, numbers to the right.
    left = 3 + int(types)
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        cells = [
            c.ljust(w) if i < left else c.rjust(w)
            for i, (c, w) in enumerate(zip(row, widths, strict=True))
        ]
        sys.stdout.write("  ".join(cells).rstrip() + "\n")


def _number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
