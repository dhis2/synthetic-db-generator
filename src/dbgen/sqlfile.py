"""Read SQL files: plain scripts, and check files of `-- name:` separated queries."""

import re
from pathlib import Path

SQL = Path(__file__).parent / "sql"


def script(path: Path) -> list[str]:
    """Split a SQL script into statements on semicolons at the end of a line."""
    text = "\n".join(line for line in path.read_text().splitlines() if not line.startswith("--"))
    return [s.strip() for s in re.split(r";\s*$", text, flags=re.MULTILINE) if s.strip()]


def checks(path: Path) -> dict[str, str]:
    """Return {name: query} of a check file."""
    found: dict[str, str] = {}
    name = None
    lines: list[str] = []
    for line in path.read_text().splitlines() + ["-- name: <end>"]:
        match = re.match(r"--\s*name:\s*(.+)$", line)
        if match:
            if name:
                found[name] = "\n".join(lines).strip().rstrip(";")
            name, lines = match.group(1).strip(), []
        elif name:
            lines.append(line)
    found.pop("<end>", None)
    return found
