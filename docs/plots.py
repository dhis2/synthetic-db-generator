"""The pictures of docs/distributions.md, drawn through dbgen's own sampler.

Each example is a line of a spec, as the guide shows it. It is compiled as a build compiles it and
drawn 100,000 times, and its histogram written to docs/img/<name>.svg. A draw a bound clips is red.
A rank picture draws 100,000 words for each value of a parameter of words.

    uv run python docs/plots.py

tests/test_docs.py fails if a picture differs from what this draws, or an example's line is not
in the guide.
"""

import math
import sys
import tomllib
from pathlib import Path

from dbgen import expr
from dbgen.distribution import DISTRIBUTIONS, Compiler
from dbgen.orgunits import connect

DOCS = Path(__file__).parent
IMG = DOCS / "img"
N = 100_000
SEED = 42

# name, the spec line, the unit of its values.
EXAMPLES = [
    ("uniform", "spacing = { uniform = [28, 35] }", "days"),
    ("normal", "height = { normal = { mean = 158, sd = 6 } }", "cm"),
    ("lognormal", "people = { lognormal = { mean = 300_000, sigma = 0.7 } }", "people"),
    ("exponential", "after = { exponential = 30 }", "days"),
    ("bernoulli", "facility_delivery = { bernoulli = 0.45 }", ""),
    ("poisson", "visits = { poisson = 4.0, plus = 1 }", "visits"),
    (
        "weights",
        "education = { weights = { NONE = 35, PRIMARY = 20, SECONDARY = 35, HIGHER = 10 } }",
        "",
    ),
    (
        "sql",
        "ga_first = { sql = \"case when u('m') < 0.35 then 6 + 7 * u('w') "
        "when u('m') < 0.75 then 13 + 9 * u('w') else 22 + 14 * u('w') end\" }",
        "weeks",
    ),
    ("truncated", "hb = { normal = { mean = 11.2, sd = 1.5 }, min = 9, max = 14 }", "g/dL"),
    ("clipped", "hb = { sql = \"11.2 + 1.5 * n('hb')\", min = 9, max = 14 }", "g/dL"),
    ("clipped-count", "visits = { poisson = 5.4, min = 1, max = 6 }", "visits"),
    (
        "overdispersed",
        "visits = { poisson = \"4.0 * exp(0.8 * n('rate') - 0.32)\", plus = 1 }",
        "visits",
    ),
    (
        "ranges",
        "age = { sql = \"case when u('range') < 0.15 then 15 + 5 * u('age') "
        "when u('range') < 0.45 then 20 + 5 * u('age') "
        "when u('range') < 0.70 then 25 + 5 * u('age') else 30 + 15 * u('age') end\" }",
        "years",
    ),
]

# name, the words a picture draws, and the key it varies with its values: a line per value.
RANKS = [
    ("words-q", {"n": 20_000, "s": 1.4}, "q", [0, 25, 100]),
    ("words-s", {"n": 20_000, "q": 25}, "s", [1.1, 1.4, 2]),
]

W, H = 560, 210
LEFT, RIGHT, TOP, BOTTOM = 16, 16, 34, 40
STYLE = """<style>
svg { --ink: #1f2933; --muted: #616e7c; --bar: #2f6f9f; --clip: #c8372d; --line: #cbd2d9; }
@media (prefers-color-scheme: dark) {
  svg { --ink: #e4e7eb; --muted: #9aa5b1; --bar: #5ea8dc; --clip: #ff6b5f; --line: #3e4c59; }
}
text { font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); }
.bar { fill: var(--bar); }
.clip { fill: var(--clip); }
text.clip { font-weight: 600; }
.axis { stroke: var(--line); }
.mark { stroke: var(--ink); stroke-dasharray: 4 3; }
.mark-label { fill: var(--ink); }
</style>"""
# The lines of a rank picture, a colour each.
LINES = """<style>
.l0 { stroke: var(--bar); }
.l1 { stroke: #b7791f; }
.l2 { stroke: #2f855a; }
@media (prefers-color-scheme: dark) {
  .l1 { stroke: #f0b453; }
  .l2 { stroke: #68d391; }
}
.l0, .l1, .l2 { fill: none; stroke-width: 2; }
.grid { stroke: var(--line); stroke-dasharray: 2 4; }
</style>"""


def draws(line: str) -> tuple[list, list[bool], dict]:
    """The values drawn for the spec line, whether a bound clipped each, and its distribution."""
    ((name, distribution),) = tomllib.loads(line).items()
    compiler = Compiler("i", lambda sql: expr.rewrite(sql, set(), "i", name))
    sql = compiler.compile(distribution, name)
    kind = next(k for k in distribution if k in DISTRIBUTIONS)
    unbounded = {k: v for k, v in distribution.items() if k not in ("min", "max")}
    raw = sql if kind == "normal" else compiler.compile(unbounded, name)
    rows = connect(SEED).execute(f"select {sql}, {raw} from range({N}) t(i)").fetchall()
    return [v for v, _ in rows], [v != r for v, r in rows], distribution


def svg(line: str, unit: str) -> str:
    """The histogram of the spec line's draws: a bar per value of whole numbers, else bins."""
    values, clipped, distribution = draws(line)
    if all(isinstance(v, str | bool) or float(v).is_integer() for v in values):
        body = bars(values, clipped, distribution)
    else:
        body = histogram(values, clipped, distribution)
    caption = text(W / 2, H - 6, unit, "middle") if unit else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img">\n<title>{escape(line)}</title>\n{STYLE}\n'
        f"{body}{caption}</svg>\n"
    )


def bars(values: list, clipped: list[bool], distribution: dict) -> str:
    """A bar per value, at most 15, the last holding the values above it too, labelled with its
    share, the share a bound clipped onto it in red."""
    if "weights" in distribution:
        keys = labels = list(distribution["weights"])
    else:
        numbers = sorted(int(v) for v in values)
        last = min(numbers[int(len(numbers) * 0.999) - 1], numbers[0] + 14)
        values = [min(int(v), last) for v in values]
        keys = list(range(numbers[0], last + 1))
        labels = [str(k) for k in keys]
        if numbers[-1] > last:
            labels[-1] += "+"
    counts: dict = {}
    clips: dict = {}
    for v, c in zip(values, clipped, strict=True):
        counts[v] = counts.get(v, 0) + 1
        clips[v] = clips.get(v, 0) + c
    peak = max(counts.values())
    slot = (W - LEFT - RIGHT) / len(keys)
    width = min(slot * 0.7, 60)
    bottom = H - BOTTOM
    out = [axis(bottom)]
    for i, (k, label) in enumerate(zip(keys, labels, strict=True)):
        n, c = counts.get(k, 0), clips.get(k, 0)
        x = LEFT + slot * i + (slot - width) / 2
        h = (n - c) / peak * (bottom - TOP)
        out.append(rect("bar", x, bottom - h, width, h))
        if c:
            hc = c / peak * (bottom - TOP)
            out.append(rect("clip", x, bottom - h - hc, width, hc))
        top = bottom - n / peak * (bottom - TOP)
        if n / len(values) >= 0.001:
            out.append(text(x + width / 2, top - 5, share(n / len(values)), "middle"))
        if c / len(values) >= 0.01:
            out.append(
                text(x + width / 2, top - 19, f"{share(c / len(values))} clipped", "middle", "clip")
            )
        out.append(text(x + width / 2, bottom + 15, label, "middle"))
    return "\n".join(out) + "\n"


def histogram(values: list, clipped: list[bool], distribution: dict) -> str:
    """The values in 50 bins over all but their 0.1% at each end, a bound's clipped share as a
    red spike at it, and markers of the mean and, where it differs, the median."""
    numbers = sorted(float(v) for v in values)
    free = [float(v) for v, c in zip(values, clipped, strict=True) if not c]
    low = numbers[int(len(numbers) * 0.001)]
    high = numbers[int(len(numbers) * 0.999) - 1]
    for bound in ("min", "max"):
        if isinstance(distribution.get(bound), int | float):
            low, high = min(low, distribution[bound]), max(high, distribution[bound])
    bins = [0] * 50
    for v in free:
        if low <= v <= high:
            bins[min(int((v - low) / (high - low) * 50), 49)] += 1
    spikes = {}
    for v, c in zip(values, clipped, strict=True):
        if c:
            spikes[float(v)] = spikes.get(float(v), 0) + 1
    peak = max([*bins, *(n / 4 for n in spikes.values())])
    bottom = H - BOTTOM
    span = W - LEFT - RIGHT

    def sx(v: float) -> float:
        return LEFT + (v - low) / (high - low) * span

    out = [axis(bottom)]
    width = span / 50
    for i, n in enumerate(bins):
        h = n / peak * (bottom - TOP)
        out.append(rect("bar", LEFT + width * i + 0.5, bottom - h, width - 1, h))
    for v, n in sorted(spikes.items()):
        h = min(n / peak, 1) * (bottom - TOP)
        out.append(rect("clip", sx(v) - 2, bottom - h, 4, h))
        anchor = "start" if sx(v) < W / 2 else "end"
        dx = 6 if anchor == "start" else -6
        out.append(
            text(
                sx(v) + dx,
                bottom - h + 12,
                f"{share(n / len(values))} at {number(v)}",
                anchor,
                "clip",
            )
        )
    for t in ticks(low, high):
        out.append(text(sx(t), bottom + 15, number(t), "middle"))
    mean = sum(numbers) / len(numbers)
    median = numbers[len(numbers) // 2]
    marks = [("mean", mean)]
    if abs(mean - median) > 0.03 * (high - low):
        marks.append(("median", median))
    for row, (label, v) in enumerate(sorted(marks, key=lambda m: m[1])):
        caption = f"{label} {number(v)}"
        anchor = "end" if sx(v) + 7 * len(caption) > W else "start"
        dx = -4 if anchor == "end" else 4
        y = TOP - 20 + 14 * row
        out.append(
            f'<line class="mark" x1="{sx(v):.1f}" x2="{sx(v):.1f}" y1="{y - 10}" y2="{bottom}"/>'
        )
        out.append(text(sx(v) + dx, y, caption, anchor, "mark-label"))
    return "\n".join(out) + "\n"


def ranks(name: str, words: dict, key: str, values: list) -> str:
    """The share of records of each word against its rank, on log scales, a line per value of
    key. It counts words, as a build writes them, so ranks whose words collide count as one."""
    height, left, top = 240, 52, 16
    bottom = height - 40
    xmax, ymin, ymax = 1000, 1e-4, 0.5
    con = connect(SEED)

    def sx(rank: float) -> float:
        return left + math.log10(rank) / math.log10(xmax) * (W - RIGHT - left)

    def sy(p: float) -> float:
        return bottom - math.log10(p / ymin) / math.log10(ymax / ymin) * (bottom - top)

    out = [f'<line class="axis" x1="{left}" x2="{W - RIGHT}" y1="{bottom}" y2="{bottom}"/>']
    for r in (1, 10, 100, 1000):
        out.append(text(sx(r), bottom + 15, f"{r:,}", "end" if r == xmax else "middle"))
    for p, label in ((0.0001, "0.01%"), (0.001, "0.1%"), (0.01, "1%"), (0.1, "10%")):
        out.append(
            f'<line class="grid" x1="{left}" x2="{W - RIGHT}" y1="{sy(p):.1f}" y2="{sy(p):.1f}"/>'
        )
        out.append(text(left - 6, sy(p) + 4, label, "end"))
    for i, v in enumerate(values):
        w = {**words, key: v}
        sql = f"word('name', rand_zipf(i, 'name', {w['n']}, {w['s']}, {w['q']}))"
        counts = con.execute(
            f"select count(*) from (select {sql} as w from range({N}) t(i)) "
            f"group by w order by 1 desc limit {xmax}"
        ).fetchall()
        points = [(k, c / N) for k, (c,) in enumerate(counts, 1) if c / N >= ymin]
        line = " ".join(f"{sx(k):.1f},{sy(p):.1f}" for k, p in points)
        out.append(f'<polyline class="l{i}" points="{line}"/>')
        y, x = top + 4 + 16 * i, W - RIGHT - 70
        out.append(f'<line class="l{i}" x1="{x}" x2="{x + 12}" y1="{y - 8}" y2="{y - 8}"/>')
        out.append(text(x + 18, y - 4, f"{key} = {v}", "start"))
    fixed = ", ".join(f"{k} = {v:,}" for k, v in words.items())
    out.append(text(W / 2, height - 6, f"rank of the word, {fixed}", "middle"))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" '
        f'height="{height}" role="img">\n<title>{escape(name)}: share of records by rank</title>\n'
        f"{STYLE}\n{LINES}\n" + "\n".join(out) + "\n</svg>\n"
    )


def ticks(low: float, high: float) -> list[float]:
    """About six round values from low to high."""
    step = (high - low) / 6
    magnitude = 10 ** math.floor(math.log10(step))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= step)
    first = math.ceil(low / step) * step
    return [first + i * step for i in range(int((high - first) / step) + 1)]


def text(x: float, y: float, content: str, anchor: str, css: str = "") -> str:
    css = f' class="{css}"' if css else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{css}>{escape(content)}</text>'


def axis(y: float) -> str:
    return f'<line class="axis" x1="{LEFT}" x2="{W - RIGHT}" y1="{y}" y2="{y}"/>'


def rect(css: str, x: float, y: float, width: float, height: float) -> str:
    return (
        f'<rect class="{css}" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" '
        f'height="{max(height, 0):.1f}"/>'
    )


def share(p: float) -> str:
    return f"{p:.0%}" if p >= 0.1 else f"{p:.1%}"


def number(v: float) -> str:
    """A tick or a marker: 1.5M, 300k, 158, 11.2."""
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.3g}M"
    if abs(v) >= 10_000:
        return f"{v / 1000:.3g}k"
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.3g}"


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write(directory: Path = IMG) -> None:
    """Write every example's picture to directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, line, unit in EXAMPLES:
        (directory / f"{name}.svg").write_text(svg(line, unit))
    for name, words, key, values in RANKS:
        (directory / f"{name}.svg").write_text(ranks(name, words, key, values))


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else IMG)
