from datetime import date

import duckdb
import pytest

from dbgen import distribution, sqlfile
from dbgen.errors import UsageError

N = 200_000


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect()
    con.execute("set variable seed = 42")
    for statement in sqlfile.script(sqlfile.SQL / "macros.sql"):
        con.execute(statement)
    return con


def moments(con, spec):
    sql = distribution.Compiler("i").compile(spec, "x")
    return con.execute(
        f"select avg(v), var_samp(v), min(v), max(v) from "
        f"(select ({sql})::double as v from range({N}) t(i))"
    ).fetchone()


@pytest.mark.parametrize(
    ("spec", "mean", "variance"),
    [
        ({"uniform": [2, 4]}, 3, 1 / 3),
        ({"normal": {"mean": 11.5, "sd": 1.5}}, 11.5, 2.25),
        ({"lognormal": {"median": 1, "sigma": 0.5}}, 1.1331, None),
        ({"lognormal": {"mean": 300_000, "sigma": 0.7}}, 300_000, None),
        ({"exponential": 30}, 30, 900),
        ({"bernoulli": 0.2}, 0.2, 0.16),
        ({"poisson": 0.19}, 0.19, 0.19),
        ({"poisson": 4.2}, 4.2, 4.2),
        ({"poisson": 120}, 120, 120),
    ],
)
def test_a_distribution_draws_its_mean_and_variance(con, spec, mean, variance):
    m, v, _, _ = moments(con, spec)
    assert m == pytest.approx(mean, rel=0.03)
    if variance is not None:
        assert v == pytest.approx(variance, rel=0.06)


def test_a_normal_with_min_and_max_is_truncated_not_clipped(con):
    m, _, low, high = moments(con, {"normal": {"mean": 11.5, "sd": 1.5}, "min": 11.5, "max": 18})
    assert low >= 11.5 and high <= 18
    # The mean of a half normal, not a pile on the bound.
    assert m == pytest.approx(11.5 + 1.5 * 0.7979, rel=0.01)


def test_plus_adds_to_the_value_drawn(con):
    m, v, low, _ = moments(con, {"poisson": 0.19, "plus": 1})
    assert low == 1
    assert m == pytest.approx(1.19, rel=0.02)
    assert v == pytest.approx(0.19, rel=0.06)


def test_min_and_max_bound_the_value_with_plus(con):
    _, _, low, high = moments(con, {"poisson": 4, "plus": 1, "max": 3})
    assert (low, high) == (1, 3)
    # A normal moved by plus, then truncated: the mean of a half normal below 10.
    m, _, _, high = moments(con, {"normal": {"mean": 0, "sd": 1}, "plus": 10, "max": 10})
    assert high <= 10
    assert m == pytest.approx(10 - 0.7979, rel=0.01)


def test_weights_draw_each_value_by_its_weight(con):
    sql = distribution.Compiler("i").compile({"weights": {"a": 3, "b": 1}}, "x")
    [(share,)] = con.execute(f"select avg((({sql}) = 'a')::int) from range({N}) t(i)").fetchall()
    assert share == pytest.approx(0.75, rel=0.02)


def test_a_zipf_rank_lies_from_1_to_n_and_favours_the_first(con):
    # The ranks of words, which draw a made-up word by its Zipf rank.
    counts = dict(con.execute(f"select v, count(*) from (select rand_zipf(i, 'x', 10, 1.2, 0) as v "
                              f"from range({N}) t(i)) group by v").fetchall())  # fmt: skip
    assert min(counts) == 1 and max(counts) == 10
    assert counts[1] > counts[2] > counts[10]


def test_an_offset_makes_the_first_ranks_alike_and_keeps_the_tail(con):
    def ranks(q):
        sql = f"select rand_zipf(i, 'x', 10000, 1.5, {q}) as v from range({N}) t(i)"
        return dict(con.execute(f"select v, count(*) from ({sql}) group by v").fetchall())

    zipf, shifted = ranks(0), ranks(50)
    assert min(shifted) >= 1 and max(shifted) <= 10_000
    assert shifted[1] < zipf[1] / 5
    assert shifted[1] / shifted[2] == pytest.approx(1, abs=0.15)
    # Far beyond the offset, the weight still falls as k^-s: a range twice as far out, and twice
    # as wide, holds 2^(1-s) as many draws.
    near = sum(c for k, c in shifted.items() if 1000 <= k < 2000)
    far = sum(c for k, c in shifted.items() if 2000 <= k < 4000)
    assert near / far == pytest.approx(2**0.5, rel=0.06)


@pytest.mark.parametrize("q", [0, 50])
def test_s_of_1_draws_as_s_just_above_1(con, q):
    # s = 1 is drawn by a branch of its own, the limit of the other: a key draws the same rank.
    rank = "rand_zipf(i, 'x', 10000, {}, " + str(q) + ")"
    [(differ,)] = con.execute(
        f"select count(*) filter (where {rank.format(1)} <> {rank.format(1 + 1e-7)}) "
        f"from range({N}) t(i)"
    ).fetchall()
    assert differ < N / 1000


def test_q_of_0_draws_the_ranks_of_before(con):
    # Pinned: other ranks change the words of every build that sets no q.
    ranks = "select list(rand_zipf(i, 'x', 5000, {}, 0) order by i) from range(8) t(i)"
    assert con.execute(ranks.format(1.1)).fetchone() == ([782, 4, 15, 84, 521, 147, 2711, 1451],)
    assert con.execute(ranks.format(1)).fetchone() == ([1373, 9, 35, 204, 1001, 342, 3351, 2170],)


def test_the_same_key_and_label_draw_the_same_and_other_labels_not(con):
    a = distribution.Compiler("i").compile({"uniform": [0, 1]}, "a")
    b = distribution.Compiler("i").compile({"uniform": [0, 1]}, "b")
    [(same, corr)] = con.execute(
        f"select bool_and({a} = {a}), corr({a}, {b}) from range({N}) t(i)"
    ).fetchall()
    assert same
    assert abs(corr) < 0.01


def test_a_uniform_stays_below_1_for_the_largest_draw():
    con = duckdb.connect()
    for statement in sqlfile.script(sqlfile.SQL / "macros.sql"):
        con.execute(statement)
    con.execute("create or replace macro draw(key, label) as 18446744073709551615::ubigint")
    exponential = distribution.Compiler("1").compile({"exponential": 1}, "x")
    row = con.execute(f"select rand_u(1, 'x'), {exponential}").fetchone()
    assert row is not None
    u, e = row
    assert u < 1
    assert e < 40


def test_dates_are_days(con):
    # 2020-01-02 is day 18263 since 1970-01-01.
    assert moments(con, date(2020, 1, 2))[0] == 18263
    _, _, low, high = moments(con, {"uniform": [date(2020, 1, 2), date(2020, 1, 3)]})
    assert 18263 <= low and high <= 18264


def test_a_string_is_text_as_a_value_and_an_expression_as_a_parameter(con):
    c = distribution.Compiler("i")
    assert c.compile("2w", "x") == "'2w'"
    assert c.compile({"weights": {"12m": 1}}, "x") == "'12m'"
    sql = c.compile({"exponential": "gap"}, "x")
    [(mean,)] = con.execute(
        f"select avg(({sql})::double) from (select i, 30 as gap from range({N}) t(i))"
    ).fetchall()
    assert mean == pytest.approx(30, rel=0.03)


@pytest.mark.parametrize(
    "spec",
    [
        {"normal": {"mean": 1}},
        {"uniform": [1]},
        {"poisson": 1, "bernoulli": 0.5},
        {"poison": 1},
        {"beta": [2, 5]},
        {"poisson": 1, "bounds": [0, 2]},
        {"weights": {}},
        {"uniform": [5, 1]},
        {"uniform": [date(2021, 1, 1), date(2020, 1, 1)]},
        {"normal": {"mean": 1, "sd": -1}},
        {"lognormal": {"median": 0, "sigma": 1}},
        {"lognormal": {"mean": 10, "sigma": -0.5}},
        {"exponential": -1},
        {"bernoulli": 1.5},
        {"poisson": -0.1},
    ],
)
def test_a_malformed_distribution_fails_with_its_place(spec):
    with pytest.raises(UsageError, match="program.preg.values.x"):
        distribution.Compiler("i").compile(spec, "label", "program.preg.values.x")


def test_mean_of_reads_constant_parameters():
    assert distribution.mean_of({"poisson": 4.2}) == 4.2
    assert distribution.mean_of({"uniform": [2, 4], "plus": 1}) == 4
    assert distribution.mean_of({"poisson": "district.people"}) is None


@pytest.mark.parametrize(
    "spec",
    [{"uniform": [1]}, {"uniform": {}}, {"normal": 3}, {"lognormal": {"sigma": 1}}, {"x": 1}],
)
def test_mean_of_a_distribution_of_malformed_parameters_is_none(spec):
    assert distribution.mean_of(spec) is None


def test_kind_of_names_the_distribution():
    assert distribution.kind_of({"poisson": 2, "plus": 1}) == "poisson"
    assert distribution.kind_of(3) == "constant"
    assert distribution.kind_of({"poisson": 2, "normal": {}}) is None
    assert distribution.kind_of({"plus": 1}) is None
