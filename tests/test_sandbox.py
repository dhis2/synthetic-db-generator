import duckdb
import pytest

from dbgen import sandbox


def confined(*directories):
    con = duckdb.connect()
    sandbox.confine(con, directories)
    return con


def test_a_file_under_a_directory_is_read_and_written(tmp_path):
    con = confined(tmp_path)
    con.execute("copy (select 42 as x) to ?", [str(tmp_path / "x.parquet")])
    assert con.execute(
        "select x from read_parquet(?)", [str(tmp_path / "x.parquet")]
    ).fetchall() == [(42,)]


def test_a_file_outside_the_directories_is_neither_read_nor_written(tmp_path):
    (tmp_path / "allowed").mkdir()
    secret = tmp_path / "allowedness.txt"
    secret.write_text("secret")
    con = confined(tmp_path / "allowed")
    with pytest.raises(duckdb.PermissionException):
        con.execute("select content from read_text(?)", [str(secret)])
    with pytest.raises(duckdb.PermissionException):
        con.execute("copy (select 1) to ?", [str(tmp_path / "out.csv")])
    with pytest.raises(duckdb.PermissionException):
        con.execute(
            "select content from read_text(?)", [str(tmp_path / "allowed/../allowedness.txt")]
        )


def test_no_url_is_read():
    with pytest.raises(duckdb.PermissionException):
        confined().execute("select content from read_text('https://example.org')")


@pytest.mark.parametrize(
    "setting",
    ["enable_external_access = true", "memory_limit = '64GB'", "allowed_directories = ['/']"],
)
def test_no_setting_changes_after(setting):
    with pytest.raises(duckdb.InvalidInputException, match="configuration has been locked"):
        confined().execute(f"set {setting}")
