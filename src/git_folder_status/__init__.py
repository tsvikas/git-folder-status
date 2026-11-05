"""git-folder-status: Find all subdirectories with uncommitted or unpushed code.

© 2025 Tsvika Shapira. Some rights reserved.
"""

from ._version import version as _version
from .format import (
    REPORT_FORMATS_TYPE,
    format_report,
)
from .git_folder_status import (
    DEFAULT_JOBS,
    RepoStats,
    issues_for_all_subfolders,
)

__version__ = _version
__all__: list[str] = [
    "DEFAULT_JOBS",
    "REPORT_FORMATS_TYPE",
    "RepoStats",
    "format_report",
    "issues_for_all_subfolders",
]
