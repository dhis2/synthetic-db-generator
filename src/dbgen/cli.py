"""Build a synthetic DHIS2 tracker database from a spec and its metadata.

Try a spec with areas, explain and sample, which need no Docker, check it against DHIS2's program
rules with validate, then build its database. `dbgen <command> -h` tells more of a command.
"""

import argparse
import shutil
import sys
from pathlib import Path

from dbgen.config import Build, shown
from dbgen.errors import InternalError, UsageError

SPEC_HELP = "the spec, a .toml file such as examples/pregnancy/pregnancy.toml"

# After an InternalError, which running dbgen differently does not fix.
INTERNAL = (
    "This needs a fix in dbgen, not in how it was run: in its code, or the DHIS2 image it pins."
)

EXAMPLES = """\
examples:
  dbgen areas examples/pregnancy/pregnancy.toml
  dbgen explain examples/pregnancy/pregnancy.toml --program PREGNANCY
  dbgen sample examples/pregnancy/pregnancy.toml --orgunit EX0201
  dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
"""

STAGES = {
    "compile": "write the effective spec and its SQL, checked against metadata",
    "orgunits": "draw the org unit hierarchy",
    "users": "make the users and their user groups",
    "generate": "draw the tracker tables with metadata UIDs, run pre-load checks",
    "bootstrap": "start DHIS2 on an empty database, import metadata, get ID maps",
    "load": "bulk load the tracker tables, UIDs swapped for internal IDs",
    "verify": "post-load checks, importer validation of a sample, smoke queries",
    "dump": "write the SQL dump and its manifest to output/<build>/",
}

STAGE_INPUTS = """\
what each stage reads and writes:
  stage      reads                               writes
  compile    the spec, the metadata              output/stage/<build>/compile/
  orgunits   compile                             orgunits/
  users      compile, orgunits                   users/
  generate   compile, orgunits, users            generate/
  bootstrap  compile, orgunits, users            bootstrap/, the build database
  load       generate, bootstrap                 load/, the build database
  verify     load, the build database            verify/
  dump       generate, load, verify              output/<build>/
"""


class HelpFormatter(argparse.HelpFormatter):
    """Fills each paragraph to the terminal's width but keeps the indented ones, such as a table,
    and lists the commands under their title, without argparse's line of their names above them.
    """

    def __init__(self, prog: str, **kwargs) -> None:
        kwargs.setdefault("width", min(shutil.get_terminal_size().columns - 2, 100))
        super().__init__(prog, **kwargs)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        return "\n\n".join(
            paragraph
            if any(line.startswith(" ") for line in paragraph.splitlines())
            else super(HelpFormatter, self)._fill_text(paragraph, width, indent)
            for paragraph in text.strip().split("\n\n")
        )

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return "".join(self._format_action(a) for a in action._get_subactions())
        return super()._format_action(action)

    def _iter_indented_subactions(self, action: argparse.Action):
        if isinstance(action, argparse._SubParsersAction):
            yield from action._get_subactions()
        else:
            yield from super()._iter_indented_subactions(action)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbgen", description=__doc__, epilog=EXAMPLES, formatter_class=HelpFormatter
    )
    commands = parser.add_subparsers(
        title="commands", dest="command", required=True, metavar="<command>"
    )

    def command(name: str, help: str, description: str, **kwargs) -> argparse.ArgumentParser:
        return commands.add_parser(
            name, help=help, description=description, formatter_class=HelpFormatter, **kwargs
        )

    areas = command(
        "areas",
        "list org units, with the records a build of each holds",
        "List an org unit, the root by default, and those of the level below, with the tracked "
        "entities and events a build of each would hold. Their codes are what --orgunit takes.",
    )
    areas.add_argument("spec", type=Path, help=SPEC_HELP)
    areas.add_argument(
        "--orgunit",
        dest="code",
        metavar="CODE",
        help="the org unit to list, with those of the level below (default: the root)",
    )
    areas.set_defaults(func=cmd_areas)

    explain = command(
        "explain",
        "print the effective spec, field generators and expected records",
        "Print the effective spec: every field that gets a value with its generator and where that "
        "came from, how many optional fields are left empty, and the records it expects. Fails, "
        "as compile does, on a name, option or expression the build would fail on. Needs no "
        "Docker.",
    )
    _build_args(explain)
    explain.add_argument("--program", help="print only this program, by code, UID or name")
    explain.add_argument("--json", action="store_true", help="print the effective spec as JSON")
    explain.set_defaults(func=cmd_explain)

    sample = command(
        "sample",
        "generate the records of an area as Parquet, without Docker",
        "Run compile, orgunits, users and generate over an area, without Docker, into Parquet "
        "files to query with DuckDB.",
    )
    _build_args(sample)
    sample.set_defaults(func=cmd_sample)

    validate = command(
        "validate",
        "check an area's records against DHIS2's program rules",
        "Run sample, then bootstrap, and submit every record of the area to DHIS2's tracker "
        "importer, which runs the program rules the spec must satisfy.",
    )
    _build_args(validate)
    validate.set_defaults(func=cmd_validate)

    build = command(
        "build",
        "run every stage; empties the build database",
        "Run every stage, from an empty build database, over the org units under --orgunit; "
        "without it, over all of them. The dump and its manifest end up in output/<build>/.",
        epilog=STAGE_INPUTS,
    )
    _build_args(build)
    build.set_defaults(func=cmd_build)

    stage = command(
        "stage",
        "run one stage of the pipeline",
        "Run one stage, once the stages it reads have run.",
        epilog=STAGE_INPUTS,
    )
    stage_commands = stage.add_subparsers(
        title="stages", dest="stage", required=True, metavar="<stage>"
    )
    for name, help_text in STAGES.items():
        p = stage_commands.add_parser(
            name,
            help=help_text,
            description=help_text[0].upper() + help_text[1:] + ".",
            formatter_class=HelpFormatter,
        )
        _build_args(p)
        p.set_defaults(func=cmd_stage)
        if name == "bootstrap":
            p.add_argument(
                "--reset",
                action="store_true",
                help="drop the build database, to start from an empty one",
            )
        if name == "load":
            p.add_argument(
                "--replace",
                action="store_true",
                help="empty the tracker tables first, to load them again",
            )

    describe = command(
        "describe",
        "print what a Parquet file holds and what each column is",
        "Print what a Parquet file holds, such as a stage's output: its rows, metadata, and each "
        "column's type and description.",
    )
    describe.add_argument(
        "file",
        type=Path,
        metavar="FILE",
        help="a Parquet file, such as output/stage/<build>/generate/event.parquet",
    )
    describe.set_defaults(func=cmd_describe)

    lock = command(
        "lock",
        "check the metadata against its packages, or take a new version",
        "Check the packages pinned in databases/<name>/packages.lock.toml against their metadata "
        "files, or take a new version of them. To take one, change its url in the "
        "packages.lock.toml, run --update and review the diff of its metadata file.",
    )
    lock.add_argument(
        "names",
        nargs="*",
        metavar="PACKAGE",
        help="a package of a packages.lock.toml, such as anc (default: all, for --check)",
    )
    lock.add_argument(
        "--check",
        action="store_true",
        help="download each package, check its sha256 against the pin and the first commit of "
        "its metadata file against the package (default)",
    )
    lock.add_argument(
        "--update",
        action="store_true",
        help="download each package, pin its sha256 and extract it over its metadata file",
    )
    lock.set_defaults(func=cmd_lock)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except UsageError as e:
        print(f"dbgen: {e}", file=sys.stderr)
        sys.exit(2)
    except InternalError as e:
        print(f"dbgen: {e}\n\n{INTERNAL}", file=sys.stderr)
        sys.exit(1)


def _build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("spec", type=Path, help=SPEC_HELP)
    parser.add_argument(
        "--orgunit",
        dest="codes",
        action="extend",
        nargs="+",
        default=[],
        metavar="CODE",
        help="the org units whose records to build, by code as dbgen areas lists them; several "
        "need --name (default: all)",
    )
    parser.add_argument(
        "--name",
        help="the name of an area of several org units, such as large, for the build's name "
        "<spec>-<name>",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        metavar="DIR",
        help="the directory to write output/ in (default: the working directory)",
    )


def _build(args: argparse.Namespace) -> Build:
    try:
        return Build(spec=args.spec, codes=tuple(args.codes), area_name=args.name, root=args.root)
    except ValueError as e:
        raise UsageError(str(e)) from e


def run_stage(build: Build, name: str, **options) -> None:
    from dbgen import compile, generate, orgunits, users
    from dbgen.dhis2 import bootstrap, client, dump, load, verify

    if name == "bootstrap" and options.get("reset"):
        client.reset()
    runners = {
        "compile": compile.run,
        "orgunits": orgunits.run,
        "users": users.run,
        "bootstrap": bootstrap.run,
        "generate": generate.run,
        "load": lambda b: load.run(b, replace=options.get("replace", False)),
        "verify": verify.run,
        "dump": dump.run,
    }
    runners[name](build)


def cmd_build(args: argparse.Namespace) -> None:
    """Every stage in order, from a clean build database."""
    build = _build(args)
    for name in STAGES:
        run_stage(build, name, reset=True)


def cmd_stage(args: argparse.Namespace) -> None:
    run_stage(
        _build(args),
        args.stage,
        reset=getattr(args, "reset", False),
        replace=getattr(args, "replace", False),
    )


def cmd_explain(args: argparse.Namespace) -> None:
    from dbgen import explain

    explain.run(_build(args), program=args.program, as_json=args.json)


def cmd_sample(args: argparse.Namespace) -> None:
    build = _build(args)
    for name in ("compile", "orgunits", "users", "generate"):
        run_stage(build, name)
    print(
        f"sample: the records are Parquet files in {shown(build.stage_dir('generate'))}/, to "
        "query with DuckDB",
        file=sys.stderr,
    )


def cmd_areas(args: argparse.Namespace) -> None:
    from dbgen import areas

    try:
        build = Build(spec=args.spec)
    except ValueError as e:
        raise UsageError(str(e)) from e
    areas.run(build, args.code)


def cmd_validate(args: argparse.Namespace) -> None:
    from dbgen.dhis2 import validate

    validate.run(_build(args))


def cmd_describe(args: argparse.Namespace) -> None:
    from dbgen import describe

    describe.run(args.file)


def cmd_lock(args: argparse.Namespace) -> None:
    from dbgen import lock

    if args.update:
        if not args.names:
            raise UsageError(
                "--update needs the packages to update, such as: dbgen lock --update anc"
            )
        lock.update(args.names)
    else:
        lock.check(args.names)


if __name__ == "__main__":
    main()
