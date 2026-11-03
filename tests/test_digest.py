import duckdb

from dbgen import digest

ROWS = "select * from (values (1, 'a', 11.2), (2, 'b', 9.8), (3, 'c', 12.0)) t(k, uid, hb)"


def row_digest(query):
    return digest.row_digest(duckdb.connect(), query)


def test_the_order_of_the_rows_does_not_count():
    assert row_digest(f"{ROWS} order by k") == row_digest(f"{ROWS} order by k desc")


def test_a_changed_value_changes_the_digest():
    changed = ROWS.replace("9.8", "10.1")
    assert row_digest(changed)["row_digest"] != row_digest(ROWS)["row_digest"]


def test_values_swapped_between_rows_change_the_digest():
    swapped = ROWS.replace("11.2", "x").replace("9.8", "11.2").replace("x", "9.8")
    assert row_digest(swapped)["row_digest"] != row_digest(ROWS)["row_digest"]


def test_a_row_present_twice_counts_twice():
    once = row_digest("select 1 as k")
    twice = row_digest("select 1 as k union all select 1")
    none = row_digest("select 1 as k where false")
    assert twice["rows"] == 2
    assert len({once["row_digest"], twice["row_digest"], none["row_digest"]}) == 3


def test_a_renamed_column_keeps_the_digest():
    renamed = f"select k, uid as id, hb from ({ROWS})"
    assert row_digest(renamed) == row_digest(ROWS)


def test_no_rows_give_zeros():
    assert row_digest(f"{ROWS} where false") == {"rows": 0, "row_digest": "0" * 16}
