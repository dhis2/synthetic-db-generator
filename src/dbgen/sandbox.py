"""The DuckDB settings that confine the spec's SQL, which dbgen runs as the spec writes it: no
file but those of the directories a stage reads and writes, no extension, and no setting changed
once they are made."""

from collections.abc import Iterable
from pathlib import Path

import duckdb


def confine(con: duckdb.DuckDBPyConnection, directories: Iterable[Path] = ()) -> None:
    """Let con read and write only the files under directories, and lock its settings. Call it
    after the settings of the stage and before the spec's SQL runs."""
    con.execute("set autoinstall_known_extensions = false")
    con.execute("set autoload_known_extensions = false")
    con.execute("set allowed_directories = ?", [[str(d) for d in directories]])
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
