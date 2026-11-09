import runpy
from pathlib import Path

DOCS = Path(__file__).parent.parent / "docs"
plots = runpy.run_path(str(DOCS / "plots.py"))


def test_the_pictures_are_what_the_sampler_draws(tmp_path):
    plots["write"](tmp_path)
    for drawn in sorted(tmp_path.iterdir()):
        committed = DOCS / "img" / drawn.name
        assert committed.exists(), f"{committed} is missing: run uv run python docs/plots.py"
        assert drawn.read_text() == committed.read_text(), (
            f"{committed} is stale: run uv run python docs/plots.py"
        )


def test_the_guide_shows_each_picture_with_its_line():
    guide = (DOCS / "distributions.md").read_text()
    for name, line, _ in plots["EXAMPLES"]:
        assert line in guide, f"docs/distributions.md lacks the line of {name}: {line}"
        assert f"img/{name}.svg" in guide, f"docs/distributions.md lacks img/{name}.svg"
    for name, *_ in plots["RANKS"]:
        assert f"img/{name}.svg" in guide, f"docs/distributions.md lacks img/{name}.svg"
