"""CLI for git_folder_status.

Run `git-folder-status -h` for help.
"""

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from . import (
    REPORT_FORMATS_TYPE,
    format_report,
    issues_for_all_subfolders,
)

app = App(name="git-folder-status")
app.register_install_completion_command()


@app.default()
def git_folder_status(  # noqa: PLR0913
    directory: Path = Path(),
    /,
    *,
    recurse: Annotated[int, Parameter(alias="-r")] = 3,
    exclude_dir: Annotated[list[str] | None, Parameter(alias="-d")] = None,
    fmt: Annotated[REPORT_FORMATS_TYPE, Parameter(name=["-f", "--format"])] = "report",
    empty: Annotated[bool, Parameter(alias="-e")] = False,
    include_all: Annotated[bool, Parameter(name=["-a", "--all"])] = False,
    slow: Annotated[bool, Parameter(alias="-s")] = False,
    include_behind: Annotated[bool, Parameter(alias="-b")] = False,
) -> int:
    """Find all unsaved data in a directory.

    Parameters
    ----------
    directory
        directory to check
    recurse
        max recurse in directories
    exclude_dir
        don't include these dirs
    fmt
        output format
    empty
        show also repos without issues
    include_all
        show other info for repos
    slow
        allow slow operations
    include_behind
        include branches that are only behind remote
    """
    issues = issues_for_all_subfolders(
        directory,
        recurse,
        exclude_dir,
        slow=slow,
        include_all=include_all,
        include_behind=include_behind,
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
