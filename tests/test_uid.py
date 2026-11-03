import duckdb
import pytest

from dbgen import uid


def test_encode_is_bijective_at_the_edges():
    assert uid.encode(0) == "a0000000000"
    assert uid.encode(uid.SPACE - 1) == "ZZZZZZZZZZZ"
    with pytest.raises(ValueError):
        uid.encode(uid.SPACE)


def test_hashed_uid_is_valid_and_stable():
    value = uid.uid("ou", "SN_BEFC69FA4D")
    assert uid.is_valid(value)
    # Pinned: another value changes the UID of every org unit and user of every build.
    assert value == "Bs6tMg3fsS5"
    assert value != uid.uid("user", "SN_BEFC69FA4D")


def test_uid_for_id_is_valid_and_does_not_collide():
    values = {uid.uid_for_id(ns, n) for ns in uid.Namespace for n in (*range(20_000), uid.MAX_ID)}
    assert len(values) == len(uid.Namespace) * 20_001
    assert all(uid.is_valid(v) for v in values)


def test_uid_for_id_rejects_out_of_range_ids():
    with pytest.raises(ValueError):
        uid.uid_for_id(uid.Namespace.EVENT, uid.MAX_ID + 1)


def test_sql_macro_matches_python():
    con = duckdb.connect()
    for macro in uid.SQL_MACROS:
        con.execute(macro)
    # Around the split of x at 62^5 and at the top of the id space, see SQL_MACROS.
    ids = [0, 1, 2, 61, 62, 12345, 62**5 - 1, 62**5, 987654321, uid.MAX_ID - 1, uid.MAX_ID]
    rows = con.execute(
        "select ns, n, dhis2_uid(ns, n) from (select unnest(?) as ns) cross join "
        "(select unnest(?) as n) order by ns, n",
        [[int(ns) for ns in uid.Namespace], ids],
    ).fetchall()
    assert rows
    for ns, n, value in rows:
        assert value == uid.uid_for_id(uid.Namespace(ns), n)
    for ns in uid.Namespace:
        [(value,)] = con.execute(f"select {ns.name.lower()}_uid(12345)").fetchall()
        assert value == uid.uid_for_id(ns, 12345)
