"""CLI for git_folder_status."""

from pathlib import Path
from typing import Annotated

import typer
from click.exceptions import UsageError

from . import __version__
from .git_folder_status import (
    REPORT_FORMATS,
    REPORT_FORMATS_TYPE,
    format_report,
    issues_for_all_subfolders,
)

app = typer.Typer()


def _version_callback(value: bool) -> None:  # noqa: FBT001
    if value:
        print(f"git-folder-status {__version__}")
        raise typer.Exit(0)


@app.command()
def git_folder_status(  # noqa: PLR0913
    directory: Annotated[Path, typer.Argument(help="directory to check")] = Path(),
    *,
    recurse: Annotated[
        int, typer.Option("-r", "--recurse", help="max recurse in directories")
    ] = 3,
    exclude_dir: Annotated[
        list[str] | None,
        typer.Option("-d", "--exclude-dir", help="don't include these dirs"),
    ] = None,
    fmt: Annotated[
        str, typer.Option("-f", "--format", help="output format")
    ] = "report",
    empty: Annotated[
        bool, typer.Option("-e", "--empty", help="show also repos without issues")
    ] = False,
    include_all: Annotated[
        bool, typer.Option("-a", "--all", help="show other info for repos")
    ] = False,
    slow: Annotated[
        bool, typer.Option("-s", "--slow", help="allow slow operations")
    ] = False,
    version: Annotated[  # noqa: ARG001
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Print version",
        ),
    ] = None,
) -> int:
    """Find all unsaved data in a directory."""
    issues = issues_for_all_subfolders(
        directory,
        recurse,
        exclude_dir,
        slow=slow,
        include_all=include_all,
    )
    if fmt not in REPORT_FORMATS:
        raise UsageError(f"format must be one of {REPORT_FORMATS}")
    fmt_report: REPORT_FORMATS_TYPE = fmt  # type: ignore[assignment]
    try:
        report = format_report(issues, include_ok=empty, fmt=fmt_report)
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Missing module for format. Try a different format or a newer python."
        ) from e
    else:
        print(report)
    return 0
