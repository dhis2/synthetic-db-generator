import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dbgen import describe, parquet
from dbgen.errors import UsageError


def test_describe_prints_each_column_with_its_type_module_unit_and_label(tmp_path, capsys):
    con = duckdb.connect()
    con.execute("create table t as select 1 as ti, 'QP1613' as district, 2.0::double as n")
    target = tmp_path / "t.parquet"
    columns = {
        **parquet.describe("stage", {"ti": ("Step", "step"), "district": "Code of the district"}),
        # a description without a label
        "n": {"module": "stats", "label": None},
    }
    parquet.copy(con, "t", target, columns)

    describe.run(target)

    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [str(target), "1 row"]
    assert "  columns  the descriptions in the table below" in lines
    assert lines[-4:] == [
        "column    type    module  unit  label",
        "ti        int32   stage   step  Step",
        "district  string  stage         Code of the district",
        "n         double  stats",
    ]


def test_describe_summarizes_long_json_and_leaves_out_the_arrow_schema(tmp_path, capsys):
    target = tmp_path / "t.parquet"
    geo = {"version": "1.0.0", "primary_column": "geom", "columns": {"geom": {"bbox": [0.0] * 20}}}
    table = pa.table({"a": [1, 2]}).replace_schema_metadata({"geo": json.dumps(geo), "n": "7"})
    pq.write_table(table, target)

    describe.run(target)

    out = capsys.readouterr().out
    assert "ARROW:schema" not in out
    assert "  n    7\n" in out
    assert (
        f"  geo  JSON of {len(json.dumps(geo))} bytes, keys: version, primary_column, columns\n"
        in out
    )


def test_describe_lists_the_columns_of_a_file_without_descriptions(tmp_path, capsys):
    target = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2], "b": ["x", "y"]}), target)

    describe.run(target)

    out = capsys.readouterr().out
    assert "2 rows\n\ncolumn  type\na       int64\nb       string\n" in out
    assert "The columns are not described" in out


def test_describe_refuses_what_is_not_a_parquet_file(tmp_path):
    text = tmp_path / "t.parquet"
    text.write_text("not parquet")

    with pytest.raises(UsageError, match="is not a Parquet file"):
        describe.run(text)
    with pytest.raises(UsageError, match="does not exist"):
        describe.run(tmp_path / "missing.parquet")
