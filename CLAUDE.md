# Conventions

## Dependencies

Prefer the Python standard library. Reach for a third-party dep only when stdlib genuinely
doesn't cover the need (e.g. generating via DuckDB, loading via psycopg, DHIS2's API via httpx).
When in doubt, ask before adding to `pyproject.toml`.

DuckDB is pinned exactly: every draw goes through its functions, so a new version can change the
data. Bump it deliberately, never as part of another change.

## Tooling

`uv` manages everything: dependencies, virtualenv, build, tests, linting. Do not suggest `pip`,
`venv`, `python -m pip`, or system-Python commands. If you need to run something, prefix with
`uv run ...` or `uv tool run ...`.

CI runs `uv run pre-commit run --all-files` (ruff, ty) and `uv run pytest`; the tests need no
Docker. To test a change end-to-end, build the example, which needs Docker and takes about 2
minutes:

```sh
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
```

## Commits

Use conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`.
