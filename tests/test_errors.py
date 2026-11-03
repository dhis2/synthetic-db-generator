"""A path or name the user gives that dbgen cannot use fails with what it is and why, not a
traceback."""

import pytest

from dbgen import cli
from tests.conftest import write_spec


def fails(capsys, *argv: str) -> str:
    """What dbgen prints when it fails with a UsageError on the arguments."""
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    assert e.value.code == 2
    return capsys.readouterr().err


def test_a_directory_as_the_spec_names_the_specs_in_it(tmp_path, capsys):
    write_spec(tmp_path, name="tiny")
    (tmp_path / "packages.lock.toml").write_text("")
    err = fails(capsys, "areas", str(tmp_path))
    assert f"spec {tmp_path} is a directory: a spec is a .toml file, such as " in err
    assert "tiny.toml" in err
    assert "packages.lock.toml" not in err


def test_explain_of_a_program_the_spec_does_not_have_names_its_programs(tmp_path, capsys):
    err = fails(capsys, "explain", str(write_spec(tmp_path)), "--program", "NOPE")
    assert "--program NOPE: no such program in the spec; its programs are " in err


def test_explain_fails_on_an_expression_compile_rejects(tmp_path, capsys):
    spec = write_spec(tmp_path, '[program.PREGNANCY.values]\nNOTES = { sql = "facility.nope" }')
    assert "does not bind (program.PREGNANCY.values.NOTES)" in fails(capsys, "explain", str(spec))


def test_a_spec_that_is_not_text_fails(tmp_path, capsys):
    spec = tmp_path / "binary.toml"
    spec.write_bytes(b"\xff\xfe")
    assert "is not UTF-8 text" in fails(capsys, "explain", str(spec))


def test_a_directory_as_a_metadata_file_fails(tmp_path, capsys):
    spec = tmp_path / "s.toml"
    spec.write_text(f'[build]\nmetadata = ["{tmp_path}"]\n')
    assert f"metadata file {tmp_path} is a directory, not a file" in fails(
        capsys, "explain", str(spec)
    )
