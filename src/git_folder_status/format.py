"""Format a report of `git_folder_status`."""

import json
import pprint
from typing import Literal

import yaml
from colorama import Fore

from .git_folder_status import RepoStats

REPORT_FORMATS_TYPE = Literal["yaml", "report", "json", "pprint"]


def format_report(
    issues: dict[str, RepoStats], *, include_ok: bool, fmt: REPORT_FORMATS_TYPE
) -> str:
    """Format report to a readable output."""
    if not include_ok:
        issues = {k: v for k, v in issues.items() if v}
    try:
        return {
            "yaml": _format_yaml,
            "report": _format_report,
            "json": _format_json,
            "pprint": _format_pprint,
        }[fmt](issues)
    except KeyError as e:
        raise ValueError(f"format_report got an unsupported {fmt=}") from e


def _format_yaml(issues: dict[str, RepoStats]) -> str:
    return yaml.dump(
        issues,
        allow_unicode=True,
        default_flow_style=False,
        indent=2,
        sort_keys=False,
    )


def _format_report(issues: dict[str, RepoStats]) -> str:
    if not issues:
        return ""

    report_lines = yaml.dump(
        issues,
        allow_unicode=True,
        default_flow_style=False,
        indent=2,
        sort_keys=False,
    ).splitlines()
    report = "\n".join(
        (
            Fore.LIGHTRED_EX + line + Fore.RESET
            if line and line[0] != " " and line[-2:] != "{}"
            else line
        )
        for line in report_lines
    )
    return report


def _format_json(issues: dict[str, RepoStats]) -> str:
    return json.dumps(issues, indent=2)


def _format_pprint(issues: dict[str, RepoStats]) -> str:
    return pprint.pformat(issues, sort_dicts=False)
