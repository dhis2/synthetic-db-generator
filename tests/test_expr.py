import re
from functools import partial

import pytest

from dbgen import distribution, expr
from dbgen.errors import UsageError

SCOPES = {"district", "facility", "enrollment", "tracked_entity", "value"}


def test_a_reference_becomes_its_column():
    assert expr.rewrite("district.people / 25_000", SCOPES) == '"district.people" / 25_000'


def test_strings_and_unknown_scopes_are_left_alone():
    sql = "case when x.y = 'facility.size' then facility.size end"
    assert expr.rewrite(sql, SCOPES) == (
        "case when x.y = 'facility.size' then \"facility.size\" end"
    )


def test_draws_take_the_record_and_label():
    assert expr.rewrite("u('a') < 0.5", SCOPES, key="k", label="trait.x") == (
        "rand_u(k, 'trait.x@a') < 0.5"
    )


def test_a_draw_of_an_expression_is_not_one_of_its_distribution():
    # rand_n(k, 'v') draws from 'v#a' and 'v#b': u('a') in its mean must draw apart from them.
    sql = distribution.Compiler("k").compile({"normal": {"mean": "u('a')", "sd": 1}}, "v")
    assert "rand_u(k, 'v@a')" in expr.rewrite(sql, SCOPES, key="k", label="v")


@pytest.mark.parametrize("name", ["x#a", "x@y", "A", "a b", ""])
def test_a_draw_name_of_other_characters_fails(name):
    with pytest.raises(UsageError, match="lowercase letters, digits and _"):
        expr.rewrite(f"u('{name}')", SCOPES, key="k", label="v")


@pytest.mark.parametrize(
    "sql",
    [
        "(select max(x) from range(3) t(x))",
        "case when u('m') < 0.35 then 6 else 1 end",
        "'a;b' || ')'",
    ],
)
def test_one_expression_passes(sql):
    expr.rewrite(sql, SCOPES)


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("0); copy (select 1) to 'x'; select (0", "it ends the statement it is written into"),
        ("0)\nfrom t; select (0", "it ends the statement it is written into"),
        ("0) from (select 1", "it adds to the statement it is written into"),
        ("0) where (true", "it adds to the statement it is written into"),
        ("0) union select (1", "it adds to the statement it is written into"),
        ("0), (1", "it adds to the statement it is written into"),
        ("0 --", "syntax error"),
    ],
)
def test_more_than_one_expression_fails(sql, problem):
    with pytest.raises(
        UsageError, match=re.escape(f"v: {sql!r} is not one DuckDB expression: {problem}")
    ):
        expr.rewrite(sql, SCOPES, label="v")


def test_references_lists_scope_and_name():
    assert expr.references("value.ANC_B6 + enrollment.lmp + 'value.x'") == {
        ("value", "ANC_B6"),
        ("enrollment", "lmp"),
    }


def test_only_the_expression_is_rewritten_not_the_key_its_draws_name():
    rewrite = partial(expr.rewrite, scopes={"p"}, key="p.code", label="l")
    sql = distribution.Compiler("p.code", rewrite).compile({"poisson": "p.people * u('a')"}, "l")
    assert '"p.people"' in sql
    assert "rand_u(p.code, 'l@a')" in sql
    assert '"p.code"' not in sql
