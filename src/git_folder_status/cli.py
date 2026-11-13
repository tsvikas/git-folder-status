"""CLI for git_folder_status.

Run `git-folder-status -h` for help.
"""

import contextlib
import os
import signal
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import cyclopts.types
from cyclopts import App, Parameter

from . import (
    DEFAULT_JOBS,
    REPORT_FORMATS_TYPE,
    format_report,
    issues_for_all_subfolders,
)

# Cyclopts would turn a Ctrl-C into `sys.exit(130)`, which waits for the scan's
# threads; `main` ends the process at once instead (see `_exit_interrupted`).
app = App(name="git-folder-status", suppress_keyboard_interrupt=False)
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
    include_unpulled: Annotated[bool, Parameter(alias="-u")] = False,
    scan_external_worktrees: Annotated[
        bool, Parameter(name=["-w", "--external-worktrees"])
    ] = False,
    jobs: Annotated[cyclopts.types.PositiveInt, Parameter(alias="-j")] = DEFAULT_JOBS,
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
        include_unpulled: include branches whose only issue is unpulled commits
            (their upstream is only `ahead`)
        scan_external_worktrees: also analyze worktrees located outside the
            scanned directory
        jobs: repos to analyze at once

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
        include_unpulled=include_unpulled,
        scan_external_worktrees=scan_external_worktrees,
        jobs=jobs,
    )
    print(format_report(issues, include_ok=empty, fmt=fmt))
    return 0


# --- Entry point ----------------------------------------------------------------------
# Maps the commands above onto exit codes, and is what `[project.scripts]` and
# `__main__` both call.

# Cyclopts itself exits 2 on invalid usage. These are sysexits(3) codes.
# `os.EX_*` holds the same values but only exists on Unix, so they are inlined
# to keep the CLI importable on Windows.
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_NOPERM = 77


def _flush_std_streams() -> None:
    """Flush stdout and stderr, which `os._exit` skips, without ever raising.

    A stream can be None (`>&-`) or fail to flush (a closed pipe), and the exit
    must still happen, with its code.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.flush()


# Both exits end the process that runs them, so their tests run them in a child
# process, which coverage does not follow.
def _exit(code: int) -> NoReturn:  # pragma: no cover
    """Exit with `code` at once, without waiting for the scan's threads.

    After an error or Ctrl-C, analyses may still be running git (an `ls-remote`
    to a dead host can take a minute), and a normal exit waits for every
    thread of the pool. The scan only reads, so nothing is lost by leaving
    them; their git processes end on their own.
    """
    _flush_std_streams()
    os._exit(code)


def _exit_interrupted() -> NoReturn:  # pragma: no cover
    """End the process on Ctrl-C at once, as killed by SIGINT, like Python does.

    A shell running the CLI in a loop stops the loop only when the CLI died
    by the signal, not when it exited with code 130.
    """
    _flush_std_streams()
    if sys.platform != "win32":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        # sent to this thread, so it is delivered before the call returns
        signal.raise_signal(signal.SIGINT)
    _exit(128 + signal.SIGINT)


def _fail(exc: Exception, code: int) -> NoReturn:
    """Report `exc` on stderr and exit with `code`."""
    print(f"error: {exc}", file=sys.stderr)
    _exit(code)


def main(tokens: Sequence[str] | None = None) -> None:
    """Run the CLI, reporting failures and mapping them onto exit codes.

    Args:
        tokens: The command line to parse. Defaults to `sys.argv[1:]`.
    """
    try:
        # `tokens` is a parameter so that tests can pass a command line here.
        # Under pytest, a bare `app()` warns, since it would parse pytest's own
        # argv, and a test that does so passes while testing nothing.
        app(tokens)
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
        _exit(EX_SOFTWARE)
    except KeyboardInterrupt:
        _exit_interrupted()
