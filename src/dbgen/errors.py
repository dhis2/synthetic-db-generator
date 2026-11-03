"""The two errors dbgen fails with, which say who fixes them.

`dbgen` prints either without a traceback (cli.main). Any other exception is a bug in dbgen and
keeps its traceback. A file the user names is checked before dbgen reads it (input_file), so that
a wrong one fails with what it is and why.
"""

from pathlib import Path


class UsageError(Exception):
    """How dbgen was run, or the machine it runs on: the user fixes it, as the message says."""


class InternalError(Exception):
    """dbgen's code, its committed inputs or the DHIS2 image it pins: a developer fixes it."""


def input_file(path: Path, what: str) -> Path:
    """The path of a file the user names for dbgen to read, such as the spec, unless it is not
    one: what names it in the message."""
    if path.is_dir():
        raise UsageError(f"{what} {path} is a directory, not a file")
    if not path.is_file():
        raise UsageError(f"{what} {path} does not exist")
    return path
