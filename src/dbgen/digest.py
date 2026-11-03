"""Row digests: whether two builds generated the same rows.

A table's row digest is the sum, modulo 2^64, of DuckDB's 64 bit hash() of each of its rows. Two
builds that generated the same rows give the same digest; any other rows give another, but for a
chance of 1 in 2^64. Its row count goes with it.

* The order of the rows does not count, so no sort is needed and the memory does not grow with the
  table. Each row holds its own key, so two rows that swap their values change the digest.
* A sum rather than a bit_xor: a row present twice counts twice, rather than cancelling out.
* hash() takes the values of a row in the order of its columns, not their names: renaming a column
  keeps the digest, adding, removing or reordering one changes it.
* DuckDB does not promise hash() stays the same across versions: 1.3 hashes a string differently
  than 1.2 did. Compare only digests taken with the same DuckDB version.
"""

import duckdb


def row_digest(con: duckdb.DuckDBPyConnection, query: str, params: list | None = None) -> dict:
    """The row count and row digest of the rows `query` returns, given `params`."""
    [(rows, total)] = con.execute(
        f"select count(*), sum(hash(q)) from ({query}) q", params
    ).fetchall()
    # sum is null without rows.
    return {"rows": rows, "row_digest": f"{(total or 0) % 2**64:016x}"}
