"""Deterministic DHIS2 UIDs.

A DHIS2 UID is 11 characters: one letter followed by 10 letters or digits (see
org.hisp.dhis.common.CodeGenerator). That gives a space of 52 * 62^10 (about 4.4e19) values.

Two schemes are used:

* uid(namespace, key) hashes a stable source key. Use it for the few thousand metadata-like rows
  (org units, users) whose identity comes from a source dataset. Collisions are checked where the
  rows are built.
* uid_for_id(namespace, n) maps an integer id bijectively into the UID space. Use it for tracker
  rows (tracked entities, enrollments, events), which are counted in the hundreds of millions at
  national scale where hashing would start to collide. The same function exists as a DuckDB macro
  (see SQL_MACROS) so transforms can compute UIDs without leaving SQL.
"""

import hashlib
from enum import IntEnum

LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALPHANUMERIC = "0123456789" + LETTERS
SPACE = len(LETTERS) * len(ALPHANUMERIC) ** 10

# Affine permutation x -> (A * x + C) mod SPACE. A is coprime to SPACE (2^12 * 13 * 31^10), so the
# mapping is a bijection and consecutive ids spread across the whole space like random UIDs do.
_A = 0x9E3779B97F4A7C15
_C = 0x632BE59BD9B4E019 % SPACE

ID_BITS = 40
MAX_ID = (1 << ID_BITS) - 1


class Namespace(IntEnum):
    """Namespaces of uid_for_id. Values are part of the output, never renumber them."""

    TRACKED_ENTITY = 1
    ENROLLMENT = 2
    EVENT = 3
    NOTE = 4
    RELATIONSHIP = 5


def encode(n: int) -> str:
    """Encode an integer in [0, SPACE) as a UID. This is a bijection."""
    if not 0 <= n < SPACE:
        raise ValueError(f"{n} is outside the UID space [0, {SPACE})")
    chars = []
    for _ in range(10):
        n, digit = divmod(n, len(ALPHANUMERIC))
        chars.append(ALPHANUMERIC[digit])
    chars.append(LETTERS[n])
    return "".join(reversed(chars))


def uid(namespace: str, key: str) -> str:
    """Return the UID for a stable source key, e.g. uid("ou", "QP1613-01-001")."""
    digest = hashlib.sha256(f"{namespace}:{key}".encode()).digest()
    return encode(int.from_bytes(digest[:16], "big") % SPACE)


def uid_for_id(namespace: Namespace, n: int) -> str:
    """Return the UID for integer id n in namespace. Distinct (namespace, n) never collide."""
    if not 0 <= n <= MAX_ID:
        raise ValueError(f"id {n} is outside [0, {MAX_ID}]")
    x = (int(namespace) << ID_BITS) | n
    return encode((_A * x + _C) % SPACE)


def is_valid(value: str) -> bool:
    return len(value) == 11 and value[0] in LETTERS and all(c in ALPHANUMERIC for c in value[1:])


_LOW = 62**5  # the permuted value splits into 6 high and 5 low characters, each fitting BIGINT
# A and C split the same way, so the SQL multiplies below 2^64 (see SQL_MACROS).
_A1, _A0 = divmod(_A, _LOW)
_C1, _C0 = divmod(_C, _LOW)
_X1_MAX = ((1 << (ID_BITS + 3)) - 1) // _LOW  # the high half of x, for namespaces below 8
assert _A1 * (_LOW - 1) + _A0 * _X1_MAX + _C1 + 51 * _LOW + (_A0 * (_LOW - 1) + _C0) // _LOW < 2**64


def _sql_char(source: str, power: int, first: bool) -> str:
    alphabet, size = (LETTERS, 52) if first else (ALPHANUMERIC, 62)
    return f"substr('{alphabet}', (({source} // {62**power}) % {size} + 1)::integer, 1)"


_CHARS = [_sql_char("s.hi", p, first=p == 5) for p in range(5, -1, -1)] + [
    _sql_char("s.lo", p, first=False) for p in range(4, -1, -1)
]

# DuckDB macro equivalent to uid_for_id. With x = x1 * L + x0, A = A1 * L + A0 and L = 62^5, the
# permuted value (A * x + C) mod 52 * L^2 is hi * L + lo with
#   lo = (A0 * x0 + C0) mod L
#   hi = ((A1 * x1 mod 52) * L + A1 * x0 + A0 * x1 + C1 + (A0 * x0 + C0) div L) mod 52 * L
# since the L^2 term only counts modulo 52. Every product fits UBIGINT: HUGEINT, which the 108-bit
# product A * x needs, divides in software and made the macro 3 times slower. The lambdas bind
# each value once, as a macro repeats its arguments wherever they appear.
SQL_MACROS = [
    "create or replace macro dhis2_uid(ns, n) as list_transform("
    f"[(ns::ubigint << {ID_BITS}) | n::ubigint], lambda x: list_transform("
    f"[{{'x1': x // {_LOW}, 'x0': x % {_LOW}}}], lambda p: list_transform("
    f"[{{'low': {_A0}::ubigint * p.x0 + {_C0}, "
    f"'high': ({_A1}::ubigint * p.x1 % 52) * {_LOW} + {_A1}::ubigint * p.x0 "
    f"+ {_A0}::ubigint * p.x1 + {_C1}}}], lambda t: list_transform("
    f"[{{'hi': (t.high + t.low // {_LOW}) % {52 * _LOW}, 'lo': t.low % {_LOW}}}], lambda s: "
    + " || ".join(_CHARS)
    + ")[1])[1])[1])[1]",
    # One macro per namespace, e.g. relationship_uid(n), so the SQL names it instead of its number.
    *(
        f"create or replace macro {ns.name.lower()}_uid(n) as dhis2_uid({int(ns)}, n)"
        for ns in Namespace
    ),
]
