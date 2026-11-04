import pytest

from dbgen import cli, provenance

A = "4e96494aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "a445a46bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.mark.parametrize(
    ("ran", "now", "change"),
    [
        ({"commit": A, "dirty": False}, {"commit": A, "dirty": False}, None),
        (
            {"commit": A, "dirty": False},
            {"commit": B, "dirty": False},
            "ran from commit 4e96494, the code is at a445a46 now",
        ),
        (
            {"commit": A, "dirty": True},
            {"commit": A, "dirty": False},
            "ran with uncommitted changes",
        ),
        (
            {"commit": A, "dirty": False},
            {"commit": A, "dirty": True},
            "the code has uncommitted changes now",
        ),
        (
            {"commit": None, "dirty": None},
            {"commit": None, "dirty": None},
            "ran from commit unknown, the code is at unknown now",
        ),
    ],
)
def test_code_change_says_how_the_code_differs(ran, now, change):
    assert provenance.code_change(ran, now) == change


def _record(commit, dirty, spec_commit=A, spec_dirty=False) -> dict:
    return {
        "commit": commit,
        "dirty": dirty,
        "spec_repository": {"commit": spec_commit, "dirty": spec_dirty},
    }


@pytest.mark.parametrize(
    ("generate", "dump", "clean"),
    [
        (_record(A, False), _record(A, False), True),
        (_record(A, False), _record(B, False), False),
        (_record(A, True), _record(A, False), False),
        (_record(A, False), _record(A, False, spec_commit=B), False),
        (_record(A, False), _record(A, False, spec_dirty=True), False),
        (_record(A, False), _record(A, False, spec_commit=None, spec_dirty=None), False),
        ({"commit": A, "dirty": False}, _record(A, False), False),
    ],
)
def test_clean_needs_one_commit_of_dbgen_and_of_the_spec_without_changes(generate, dump, clean):
    entry = {**dump, "lineage": {"generate": generate}}
    assert provenance.clean("dump", entry) is clean


def test_code_outside_dbgen_checkout_is_not_the_checkout_around_it(tmp_path, monkeypatch):
    """An install in another repository's .venv must not record that repository's commit."""
    import subprocess

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    package = tmp_path / ".venv" / "lib" / "dbgen"
    package.mkdir(parents=True)
    monkeypatch.setattr(provenance, "PACKAGE", package)
    provenance.code.cache_clear()
    try:
        state = provenance.code()
    finally:
        provenance.code.cache_clear()
    top = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert state["commit"] != top
    assert state["version"]


def test_version_names_the_commit_it_runs_from(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    code = provenance.code()
    printed = capsys.readouterr().out
    assert printed.startswith(
        f"dbgen {code['version']} (commit {(code['commit'] or 'unknown')[:7]}"
    )
