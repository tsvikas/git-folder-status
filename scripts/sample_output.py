"""Generate sample output."""

from git_folder_status.format import format_report
from git_folder_status.git_folder_status import RepoStats

report = {
    "my-repo": RepoStats({"is_dirty": True}),
    "my-other-repo": RepoStats(
        {
            "branches": {
                "main": {"ahead": 1},
                "develop": {"ahead": 3, "behind": 1},
            }
        }
    ),
    "my-3rd-repo": RepoStats({"untracked_files": ["important_file.py"]}),
    "repo-4": RepoStats({"stash_count": 1}),
}
print(format_report(report, include_ok=False, fmt="report"))
