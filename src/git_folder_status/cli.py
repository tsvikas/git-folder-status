"""CLI for git_folder_status.

Run `git-folder-status -h` for help.
"""

import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import cyclopts.types
from cyclopts import App, CycloptsError, Parameter

from . import (
    REPORT_FORMATS_TYPE,
    format_report,
    issues_for_all_subfolders,
)

app = App(name="git-folder-status")
app.register_install_completion_command()


# --- Commands -------------------------------------------------------------------------
# This is the part to replace. `@app.default()` runs when no subcommand is
# given, so switch these to `@app.command()` once there is more than one, and
# keep the exit codes each returns listed in its docstring.
@app.default()
def git_folder_status(  # noqa: PLR0913
    directory: cyclopts.types.ExistingDirectory = Path(),
    /,
    *,
    recurse: Annotated[int, Parameter(alias="-r")] = 3,
    exclude_dir: Annotated[list[str] | None, Parameter(alias="-d")] = None,
    fmt: Annotated[REPORT_FORMATS_TYPE, Parameter(name=["-f", "--format"])] = "report",
    empty: Annotated[bool, Parameter(alias="-e")] = False,
    include_all: Annotated[bool, Parameter(name=["-a", "--all"])] = False,
    slow: Annotated[bool, Parameter(alias="-s")] = False,
    include_behind: Annotated[bool, Parameter(alias="-b")] = False,
    scan_external_worktrees: Annotated[
        bool, Parameter(name=["-w", "--external-worktrees"])
    ] = False,
) -> int:
    """Find all unsaved data in a directory.

    Args:
        directory: directory to check
        recurse: max recurse in directories
        exclude_dir: don't include these dirs
        fmt: output format
        empty: show also repos without issues
        include_all: show other info for repos
        slow: allow slow operations
        include_behind: include branches that are only behind remote
        scan_external_worktrees: also analyze worktrees located outside the
            scanned directory

    Returns:
        The process exit code.

    Exit Codes:
        0: Success.
        2: Invalid usage.
        64-78: Reserved, an internal failure.
        129-159: Reserved, terminated by signal N, as 128 + N.
    """
    issues = issues_for_all_subfolders(
        directory,
        recurse,
        exclude_dir,
        slow=slow,
        include_all=include_all,
        include_behind=include_behind,
        scan_external_worktrees=scan_external_worktrees,
    )
    try:
        report = format_report(issues, include_ok=empty, fmt=fmt)
    except ModuleNotFoundError as e:
        print(
            "Missing module for format. Try a different format or a newer python.",
            file=sys.stderr,
        )
        raise SystemExit(2) from e
    else:
        print(report)
    return 0


# --- Entry point ----------------------------------------------------------------------
# Maps the commands above onto exit codes, and is what `[project.scripts]` and
# `__main__` both call.

# sysexits(3) would put usage errors at 64, but 2 is the far wider convention:
# argparse, click, clap, grep, diff, curl and bash builtins all use it.
# https://stackoverflow.com/questions/1101957/are-there-any-standard-exit-status-codes-in-linux
EX_USAGE = 2
# The rest are sysexits(3) codes. `os.EX_*` holds the same values but only
# exists on Unix, so they are inlined to keep the CLI importable on Windows.
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_NOPERM = 77


def _fail(exc: Exception, code: int) -> NoReturn:
    """Report `exc` on stderr and exit with `code`."""
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(code)


def main(tokens: Sequence[str] | None = None) -> None:
    """Run the CLI, reporting failures and mapping them onto exit codes.

    Args:
        tokens: The command line to parse. Defaults to `sys.argv[1:]`.
    """
    try:
        # `tokens` is a parameter so that tests can pass a command line here.
        # Under pytest, a bare `app()` warns, since it would parse pytest's own
        # argv, and a test that does so passes while testing nothing.
        app(tokens, exit_on_error=False)
    except CycloptsError:
        # Cyclopts has already printed its own error panel. Cyclopts >=5 exits 2
        # on parse errors itself, so once the dependency requires it, this clause
        # and `exit_on_error=False` above can both go.
        sys.exit(EX_USAGE)
    # Nothing reports the errors below, so without `_fail` the CLI would exit on
    # a bare code and no output. Match on the exception rather than on
    # `type(exc)`, so that subclasses such as ConnectionRefusedError still land
    # on the right code. Specific OSError subclasses must precede any bare
    # `except OSError`, which would otherwise swallow them.
    except FileNotFoundError as exc:
        _fail(exc, EX_NOINPUT)
    except PermissionError as exc:
        _fail(exc, EX_NOPERM)
    except ConnectionError as exc:
        _fail(exc, EX_UNAVAILABLE)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(EX_SOFTWARE)
