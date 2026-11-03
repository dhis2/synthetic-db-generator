"""The expressions of a spec: DuckDB SQL over the columns of a record and the levels above it.

An expression names a column by its scope and name, such as district.people, facility.size,
enrollment.risk or event.date. In the SQL dbgen runs, that column is a quoted identifier of the
same name, "district.people", which this module rewrites the expression to. Within an expression,
u('x') and n('x') are a uniform and a standard normal drawn for the record, under the label of
the field, @ and x. The draws dbgen derives from a label append # instead (sql/macros.sql), and a
name holds only lowercase letters, digits and _, so a draw of the spec never meets one of dbgen's.

Dates are days since 1970-01-01, as doubles, so that they add and subtract as numbers: an
expected date of delivery 40 weeks after the last menstrual period is enrollment.lmp + 280.

dbgen writes an expression into its own statements as it is, so each must be one expression: one
that closes the parentheses dbgen opens around it could add to that statement, or end it and start
one of its own.
"""

import json
import re
from functools import cache

import duckdb

from dbgen.distribution import quote
from dbgen.errors import UsageError

# A reference: a scope, a dot and a name, outside quotes.
REFERENCE = re.compile(r"\b([a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
DRAW = re.compile(r"\b([un])\('([^']*)'\)")
DRAW_NAME = re.compile(r"[a-z0-9_]+")


def rewrite(
    sql: str, scopes: set[str], key: str | None = None, label: str = "", where: str | None = None
) -> str:
    """The expression with each reference to a column of the scopes quoted, and its draws for
    `key` under `label`. Quoted strings and identifiers are left alone. The draws are written
    after the references, so the key they name is not taken for one. where names it in errors,
    the label by default."""
    problem = _not_one_expression(sql)
    if problem:
        raise UsageError(f"{where or label}: {sql!r} is not one DuckDB expression: {problem}")
    out, last = [], 0
    for m in STRING.finditer(sql):
        out.append(REFERENCE.sub(lambda r: _reference(r, scopes), sql[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(REFERENCE.sub(lambda r: _reference(r, scopes), sql[last:]))
    sql = "".join(out)
    if key is not None:
        # Over the whole expression, as a draw's name is a string.
        sql = DRAW.sub(lambda m: _draw(m.group(1), m.group(2), key, label, where or label), sql)
    return sql


@cache
def _not_one_expression(sql: str) -> str | None:
    """Why sql is not one expression, or None if it is: DuckDB parses select (sql) to one select
    that differs from select (1) in its one column alone."""
    parsed = _parse(f"select ({sql})")
    if parsed["error"] and parsed["error_type"] == "parser":
        return parsed["error_message"]
    if parsed["error"] or len(parsed["statements"]) > 1:
        return "it ends the statement it is written into"
    if _without_column(parsed) != _without_column(_parse("select (1)")):
        return "it adds to the statement it is written into"
    return None


def _without_column(parsed: dict) -> dict | None:
    """The one statement parsed, without its one column and that column's alias; None if it is no
    select of one column."""
    node = dict(parsed["statements"][0]["node"])
    if node["type"] != "SELECT_NODE" or len(node["select_list"]) != 1:
        return None
    return node | {"select_list": [node["select_list"][0]["alias"]]}


@cache
def _parse(sql: str) -> dict:
    """DuckDB's parse of sql, as json_serialize_sql gives it."""
    con = duckdb.connect(
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    )
    [(serialized,)] = con.execute("select json_serialize_sql(?)", [sql]).fetchall()
    con.close()
    return json.loads(serialized)


def _reference(m: re.Match, scopes: set[str]) -> str:
    """A reference as its column, if its scope is one of the scopes."""
    scope, name = m.group(1), m.group(2)
    return f'"{scope}.{name}"' if scope in scopes else m.group(0)


def _draw(kind: str, name: str, key: str, label: str, where: str) -> str:
    """The SQL of draw u('name') or n('name') of an expression under `label`."""
    if not DRAW_NAME.fullmatch(name):
        raise UsageError(
            f"{where}: {kind}({name!r}): a draw's name holds only lowercase letters, digits and _"
        )
    return f"rand_{kind}({key}, {quote(label + '@' + name)})"


def references(sql: str) -> set[tuple[str, str]]:
    """The (scope, name) of every reference outside quotes."""
    found = set()
    for part in STRING.split(sql):
        for m in REFERENCE.finditer(part):
            found.add((m.group(1), m.group(2)))
    return found


def column(scope: str, name: str) -> str:
    """The quoted column of a scope's trait or field, such as "district.people"."""
    return f'"{scope}.{name}"'
