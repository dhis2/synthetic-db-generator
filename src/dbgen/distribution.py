"""The distributions of a spec, each compiled to a DuckDB SQL expression over hash draws.

A distribution is a value of the spec: a constant (a number, a string, a boolean or a date), or a
table with one key naming the distribution and its parameters, and modifiers:

    constant = v
    uniform = [low, high]                   numbers or dates (2020-01-01, a TOML date)
    normal = { mean = m, sd = s }
    lognormal = { median = m, sigma = s }   or { mean = m, sigma = s }, the mean of the values
    exponential = mean
    bernoulli = p                           1 or 0, true or false as a value
    poisson = mean
    weights = { a = 3, b = 1 }              one of the keys, by weight
    sql = "expression"                      DuckDB SQL

    min, max        a normal is truncated to them, any other distribution clipped
    round = n       rounded to n decimals (0 gives an integer)
    plus = n        added to the value drawn, before min and max: { poisson = 0.19, plus = 1 } is
                    one and a Poisson number more, as a tracked entity has one enrollment and
                    maybe others

A parameter may be an expression, a string of DuckDB SQL over the record's columns and traits,
such as "district.people / 25_000" (see expr.py). Durations are numbers of days, and dates days
since 1970-01-01.

A draw is a function of the seed, the record's key and a label, the field's path in the spec
(draw in sql/macros.sql), so the same build draws the same values whatever the row order, and a
new field shifts no other's draws. A distribution taking several uniforms suffixes the label.
"""

import math
from collections.abc import Callable
from datetime import date
from typing import Any

from dbgen.errors import UsageError

DISTRIBUTIONS = (
    "constant",
    "uniform",
    "normal",
    "lognormal",
    "exponential",
    "bernoulli",
    "poisson",
    "weights",
    "sql",
)
MODIFIERS = ("min", "max", "round", "plus")

EPOCH = date(1970, 1, 1)


def days(d: date) -> int:
    """A date as days since EPOCH, as the SQL holds dates (to_days in sql/macros.sql)."""
    return (d - EPOCH).days


def quote(text: str) -> str:
    """Text as an SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


class Compiler:
    """Compiles distributions to SQL, for a record's key (an SQL expression) and labels."""

    def __init__(self, key: str, expression: Callable[[str], str] = lambda sql: sql):
        """expression: the SQL of an expression of the spec, such as expr.rewrite of its scopes.
        Only the spec's expressions go through it, not the SQL around them, which names the key."""
        self.key = key
        self.expression = expression

    def u(self, label: str) -> str:
        """A uniform in [0, 1) for the label."""
        return f"rand_u({self.key}, {quote(label)})"

    def n(self, label: str) -> str:
        """A standard normal for the label."""
        return f"rand_n({self.key}, {quote(label)})"

    def compile(self, distribution, label: str, where: str | None = None) -> str:
        """The SQL of distribution `spec`, drawn under `label`. where names it in errors."""
        where = where or label
        if not isinstance(distribution, dict):
            return self.value(distribution, where)
        names = [k for k in distribution if k in DISTRIBUTIONS]
        unknown = [k for k in distribution if k not in DISTRIBUTIONS and k not in MODIFIERS]
        if unknown:
            raise UsageError(
                f"{where}: unknown key {', '.join(unknown)}; a distribution is one of "
                f"{', '.join(DISTRIBUTIONS)}, modified by {', '.join(MODIFIERS)}"
            )
        if len(names) != 1:
            if not names and any(k in distribution for k in ("min", "max")):
                raise UsageError(f"{where}: min or max without a distribution")
            raise UsageError(
                f"{where}: needs exactly one distribution of {', '.join(DISTRIBUTIONS)}, has "
                f"{', '.join(names) or 'none'}"
            )
        name = names[0]
        sql = getattr(self, f"_{name}")(distribution[name], distribution, label, f"{where}.{name}")
        return self._modify(sql, name, distribution, where)

    def parameter(self, value, where: str) -> str:
        """A parameter as SQL: a number, a date, or an expression in parentheses."""
        if isinstance(value, bool):
            raise UsageError(f"{where}: {value!r} is not a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise UsageError(f"{where}: {value!r} is not a finite number")
        if isinstance(value, int | float):
            return repr(float(value)) if isinstance(value, float) else f"{value}::double"
        if isinstance(value, date):
            return f"{days(value)}::double"
        if isinstance(value, str):
            if not value.strip():
                raise UsageError(f"{where}: an empty expression")
            return f"(({self.expression(value)})::double)"
        raise UsageError(f"{where}: {value!r} is not a number, a date or an expression")

    def _modify(self, sql: str, name: str, distribution: dict, where: str) -> str:
        low = high = None
        if "min" in distribution:
            low = self.parameter(distribution["min"], f"{where}.min")
        if "max" in distribution:
            high = self.parameter(distribution["max"], f"{where}.max")
        if name != "normal":  # a normal is truncated where it is drawn, its mean plus moved
            if "plus" in distribution:
                sql = f"({sql} + {self.parameter(distribution['plus'], f'{where}.plus')})"
            if low is not None:
                sql = f"greatest({sql}, {low})"
            if high is not None:
                sql = f"least({sql}, {high})"
        if "round" in distribution:
            digits = distribution["round"]
            if not isinstance(digits, int) or digits < 0:
                raise UsageError(f"{where}.round: must be a number of decimals, 0 or more")
            sql = f"round({sql})::bigint" if digits == 0 else f"round({sql}, {digits})"
        return sql

    def value(self, value, where: str) -> str:
        """A constant of the spec as SQL: a number, a boolean, a date (days since 1970) or a
        string, which is text."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and not math.isfinite(value):
            raise UsageError(f"{where}: {value!r} is not a finite number")
        if isinstance(value, int | float):
            return repr(value)
        if isinstance(value, date):
            return f"{days(value)}::double"
        if isinstance(value, str):
            return quote(value)
        raise UsageError(f"{where}: {value!r} is not a value or a distribution")

    def _constant(self, value: Any, distribution: dict, label: str, where: str) -> str:
        return self.value(value, where)

    def _uniform(self, value: Any, distribution: dict, label: str, where: str) -> str:
        if not isinstance(value, list) or len(value) != 2:
            raise UsageError(f"{where}: must be [low, high]")
        low, high = (self.parameter(v, where) for v in value)
        a, b = (days(v) if isinstance(v, date) else v for v in value)
        if _number(a) and _number(b) and a > b:
            raise UsageError(f"{where}: low {value[0]!r} is above high {value[1]!r}")
        return f"({low} + {self.u(label)} * ({high} - {low}))"

    def _normal(self, value: Any, distribution: dict, label: str, where: str) -> str:
        if not isinstance(value, dict) or set(value) != {"mean", "sd"}:
            raise UsageError(f"{where}: must be {{ mean = m, sd = s }}")
        mean = self.parameter(value["mean"], where + ".mean")
        if "plus" in distribution:
            mean = f"({mean} + {self.parameter(distribution['plus'], where + '.plus')})"
        sd = self.parameter(value["sd"], where + ".sd")
        _check(value["sd"], lambda v: v >= 0, where + ".sd", "must be 0 or more")
        if "min" not in distribution and "max" not in distribution:
            return f"({mean} + {sd} * {self.n(label)})"
        low = self.parameter(distribution.get("min", -1e300), where + ".min")
        high = self.parameter(distribution.get("max", 1e300), where + ".max")
        return f"rand_tn({self.key}, {quote(label)}, {mean}, {sd}, {low}, {high})"

    def _lognormal(self, value: Any, distribution: dict, label: str, where: str) -> str:
        if not isinstance(value, dict) or "sigma" not in value or len(value) != 2:
            raise UsageError(f"{where}: must be {{ median = m, sigma = s }} or {{ mean, sigma }}")
        sigma = self.parameter(value["sigma"], where + ".sigma")
        _check(value["sigma"], lambda v: v >= 0, where + ".sigma", "must be 0 or more")
        if "median" in value:
            median = self.parameter(value["median"], where + ".median")
            _check(value["median"], lambda v: v > 0, where + ".median", "must be above 0")
        elif "mean" in value:
            mean = self.parameter(value["mean"], where + ".mean")
            _check(value["mean"], lambda v: v > 0, where + ".mean", "must be above 0")
            median = f"({mean} / exp({sigma} * {sigma} / 2))"
        else:
            raise UsageError(f"{where}: needs a median or a mean")
        return f"({median} * exp({sigma} * {self.n(label)}))"

    def _exponential(self, value: Any, distribution: dict, label: str, where: str) -> str:
        mean = self.parameter(value, where)
        _check(value, lambda v: v >= 0, where, "must be 0 or more")
        return f"(-{mean} * ln(1 - {self.u(label)}))"

    def _bernoulli(self, value: Any, distribution: dict, label: str, where: str) -> str:
        p = self.parameter(value, where)
        _check(value, lambda v: 0 <= v <= 1, where, "must be from 0 to 1")
        return f"({self.u(label)} < {p})::integer"

    def _poisson(self, value: Any, distribution: dict, label: str, where: str) -> str:
        mean = self.parameter(value, where)
        _check(value, lambda v: v >= 0, where, "must be 0 or more")
        return f"rand_poisson({self.key}, {quote(label)}, {mean})"

    def _weights(self, value: Any, distribution: dict, label: str, where: str) -> str:
        if not isinstance(value, dict) or not value:
            raise UsageError(f"{where}: must be a table of values and their weights")
        return self.categorical(list(value.items()), label, where)

    def categorical(self, weighted: list[tuple], label: str, where: str) -> str:
        """One of the values by weight: a case over cumulative weights."""
        total = 0.0
        for v, w in weighted:
            if not isinstance(w, int | float) or isinstance(w, bool) or not 0 <= w < math.inf:
                raise UsageError(f"{where}: the weight of {v!r} must be a number, 0 or more")
            total += w
        if total <= 0:
            raise UsageError(f"{where}: the weights sum to 0")
        u = self.u(label)
        cases, cumulative = [], 0.0
        for v, w in weighted[:-1]:
            cumulative += w
            cases.append(f"when {u} < {cumulative / total!r} then {self.value(v, where)}")
        last = self.value(weighted[-1][0], where)
        return f"(case {' '.join(cases)} else {last} end)" if cases else last

    def _sql(self, value: Any, distribution: dict, label: str, where: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise UsageError(f"{where}: must be a DuckDB SQL expression")
        return f"({self.expression(value)})"


def kind_of(distribution: Any) -> str | None:
    """The name of a distribution, 'constant' for a value, None for a table of no or several."""
    if isinstance(distribution, dict):
        names = [k for k in distribution if k in DISTRIBUTIONS]
        return names[0] if len(names) == 1 else None
    return "constant"


def mean_of(distribution: Any) -> float | None:
    """The mean of a distribution of numbers with constant parameters, or None: for estimates
    (explain), not for drawing."""
    if isinstance(distribution, int | float):
        return float(distribution)
    if not isinstance(distribution, dict):
        return None
    name = kind_of(distribution)
    v = distribution.get(name)
    params = v if isinstance(v, dict) else {}
    if name in ("constant", "poisson", "bernoulli", "exponential") and _number(v):
        m = float(v)
    elif name == "uniform" and isinstance(v, list) and len(v) == 2 and all(map(_number, v)):
        m = (v[0] + v[1]) / 2
    elif name == "normal" and _number(params.get("mean")):
        m = float(params["mean"])
    elif name == "lognormal" and _number(params.get("sigma")) and _number(params.get("mean")):
        m = float(params["mean"])
    elif name == "lognormal" and _number(params.get("sigma")) and _number(params.get("median")):
        m = float(params["median"]) * math.exp(params["sigma"] ** 2 / 2)
    else:
        return None
    if isinstance(distribution.get("plus"), int | float):
        m += distribution["plus"]
    if isinstance(distribution.get("min"), int | float):
        m = max(m, distribution["min"])
    if isinstance(distribution.get("max"), int | float):
        m = min(m, distribution["max"])
    return m


def _number(value: Any) -> bool:
    return isinstance(value, int | float)


def _check(value: Any, ok: Callable[[float], bool], where: str, must: str) -> None:
    """Fail on a constant parameter out of its range. An expression is only known when drawn."""
    if _number(value) and not ok(value):
        raise UsageError(f"{where}: {value!r} {must}")
