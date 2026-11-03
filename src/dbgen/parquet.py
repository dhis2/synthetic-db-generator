"""What each column of a Parquet file holds, under the key `columns` of its schema metadata.

Every Parquet file dbgen writes describes each of its columns there: the module that made it,
what it holds (label) and, if it has one, its unit. A module is a Python class or module, an SQL
script or a mapping, or what dbgen reads it from, such as DHIS2. Writing fails on a column
without a description, and on a description without a column.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import duckdb

from dbgen.errors import InternalError


def describe(module: str, labels: Mapping[str, str | tuple[str, str]]) -> dict[str, dict[str, str]]:
    """The descriptions of the columns `module` made, from the label of each, or its label and
    unit."""
    columns = {}
    for name, label in labels.items():
        label, unit = label if isinstance(label, tuple) else (label, "")
        columns[name] = {"module": module, "label": label} | ({"unit": unit} if unit else {})
    return columns


def check(name: str, names: list[str], columns: dict[str, dict]) -> None:
    """Fail on a column of file `name` without a description, or a description without a
    column."""
    undescribed = set(names) - set(columns)
    unknown = set(columns) - set(names)
    if undescribed or unknown:
        raise InternalError(
            f"{name}: columns without a description {sorted(undescribed)}, "
            f"descriptions without a column {sorted(unknown)}"
        )


def copy(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    target: Path,
    columns: dict[str, dict],
    options: str = "",
) -> int:
    """Write `relation`, a table or a query in parentheses, to Parquet file `target` with its
    columns described in the order they come, and return the number of rows written. `options`
    adds to those of DuckDB's copy, such as "compression zstd"."""
    names = [d[0] for d in con.execute(f"select * from {relation} limit 0").description]
    check(target.name, names, columns)
    metadata = json.dumps({name: columns[name] for name in names})
    settings = "format parquet, kv_metadata {columns: ?}" + (f", {options}" if options else "")
    [(rows,)] = con.execute(
        f"copy {relation} to ? ({settings})", [str(target), metadata]
    ).fetchall()
    return rows
