"""`dbgen describe <file.parquet>`: what a Parquet file holds.

Prints the path and the number of rows, the key-value metadata but pyarrow's serialized schema
(ARROW:schema) and the key `columns`, and a table of the columns: each one's type and the
description the stage wrote under `columns` (see parquet.py), its module, unit and label. Reads only
the file's footer.
"""

import json
import shutil
import textwrap
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from dbgen.errors import UsageError, input_file

# The longest metadata value printed whole. A longer one is cut, and JSON summarized by its keys,
# such as GeoParquet's `geo`.
MAX_VALUE = 80
# The narrowest a label wraps to, however narrow the terminal.
MIN_LABEL = 40
# What the table shows of each column's description.
DESCRIPTION = ("module", "unit", "label")


def run(path: Path) -> None:
    input_file(path, "file")
    try:
        file = pq.ParquetFile(path)
    except pa.ArrowInvalid as e:
        raise UsageError(f"{path} is not a Parquet file: {e}") from e
    metadata = {
        key.decode(): value.decode(errors="replace")
        for key, value in (file.metadata.metadata or {}).items()
        if key != b"ARROW:schema"
    }
    print(path)
    rows = file.metadata.num_rows
    print(f"{rows:,} {'row' if rows == 1 else 'rows'}")
    if metadata:
        print("\nmetadata")
        width = max(len(key) for key in metadata)
        for key, value in metadata.items():
            shown = "the descriptions in the table below" if key == "columns" else _summary(value)
            print(f"  {key:<{width}}  {shown}")
    print()
    fields = list(file.schema_arrow)
    if "columns" not in metadata:
        _print_table(("column", "type"), [(f.name, str(f.type)) for f in fields])
        print("\nThe columns are not described: the metadata has no key `columns`.")
        return
    described = json.loads(metadata["columns"])
    _print_table(
        ("column", "type", *DESCRIPTION),
        [
            (f.name, str(f.type), *(described.get(f.name, {}).get(k) or "" for k in DESCRIPTION))
            for f in fields
        ],
    )


def _summary(value: str) -> str:
    """The value, or if longer than MAX_VALUE its start, and of a JSON object its size and keys."""
    if len(value) > MAX_VALUE:
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = f"JSON of {len(value):,} bytes, keys: {', '.join(parsed)}"
    return value if len(value) <= MAX_VALUE else value[: MAX_VALUE - 3] + "..."


def _print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """Print the rows in aligned columns, the last wrapped to the terminal's width."""
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers) - 1)]
    indent = sum(widths) + 2 * len(widths)
    last = max(MIN_LABEL, shutil.get_terminal_size().columns - indent)
    for row in [headers, *rows]:
        lead = "".join(f"{cell:<{width}}  " for cell, width in zip(row, widths, strict=False))
        lines = textwrap.wrap(row[-1], last) or [""]
        print((lead + lines[0]).rstrip())
        for line in lines[1:]:
            print(" " * indent + line)
