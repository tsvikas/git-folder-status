#!/usr/bin/env python3
"""
Find all subdirectories with uncommitted or unpushed code.

This script scans through a directory recursively to identify the status of
all Git repositories found within.

It returns a list of repos/submodules with their issues, including:
* uncommitted changes
* untracked files
* stash entries
* detached head
* branches without remote
* branches with unpushed changes
* tags without a remote tag (only with -s/--slow)
* tags that differ from their remote tag (only with -s/--slow)
* directories with any content that is not part of a repo.
* broken links outside repos


Run `git-folder-status -h` for help.
Requires GitPython package
"""

import argparse
from pathlib import Path
from typing import Any, Literal

from git import InvalidGitRepositoryError, Repo
from git.refs.head import Head


def shorten_list(items: list[str], limit: int = 10) -> list[str]:
    if len(items) <= limit:
        return items
    short_list = items[: limit // 2] + items[-limit // 2 :]
    short_list[limit // 2] = f"<< {len(items) - limit + 1} more items >>"
    return short_list


def repo_stats(repo: Repo) -> dict[str, Any]:
    untracked_files = shorten_list(repo.untracked_files)
    head = repo.head
    try:
        commit = head.commit
    except ValueError:
        # handle an empty repo
        commit = None
    return {
        "is_dirty": repo.is_dirty(),
        "untracked_files": untracked_files,
        "is_detached_head": head.is_detached,
        "active_branch": None if head.is_detached else repo.active_branch.name,
        "head_commit_hash_short": commit.hexsha[:7] if commit else None,
        "branches": {b.name: b.commit.hexsha for b in repo.branches},
        "remotes": {r.name: list(r.urls) for r in repo.remotes},
        "stash_count": len(repo.git.stash("list").splitlines()),
    }


def repo_issues_in_stats(repo: Repo, slow: bool) -> dict[str, Any]:
    stats_to_include = {
        "is_dirty",
        "untracked_files",
        "stash_count",
        "is_detached_head",
    }
    stats = repo_stats(repo)
    issues = {k: stats.get(k, None) for k in stats_to_include}
    issues = {k: v for k, v in issues.items() if v}
    return issues


def branch_status(repo: Repo, branch: Head) -> dict[str, Any]:
    if branch.tracking_branch() is None:
        return {"remote_branch": False}
    local_branch = branch.name
    remote_branch = branch.tracking_branch().name
    if remote_branch[0] == ".":
        # tracking a local branch
        return {"remote_branch": False}
    commits_behind = repo.iter_commits(f"{local_branch}..{remote_branch}")
    commits_ahead = repo.iter_commits(f"{remote_branch}..{local_branch}")
    return {
        "remote_branch": remote_branch,
        "commits_behind": len(list(commits_behind)),
        "commits_ahead": len(list(commits_ahead)),
    }


def all_branches_status(repo: Repo) -> dict[str, dict[str, Any]]:
    return {branch.name: branch_status(repo, branch) for branch in repo.branches}


def repo_issues_in_branches(repo: Repo, slow: bool) -> dict[str, Any]:
    branches_st = all_branches_status(repo)
    issues: dict[str, Any] = {}
    issues["branches_without_remote"] = [
        k for k, v in branches_st.items() if not v["remote_branch"]
    ]
    issues["branches_out_of_sync"] = {
        k: v
        for k, v in branches_st.items()
        if v["remote_branch"] and (v["commits_behind"] or v["commits_ahead"])
    }
    issues = {k: v for k, v in issues.items() if v}
    return issues


def repo_issues_in_tags(repo: Repo, slow: bool) -> dict[str, Any]:
    assert "origin" in repo.remotes, f"repo has no remote origin, only {repo.remotes}"
    local_tags = {tag.path: tag.commit.hexsha for tag in repo.tags}
    issues = {}
    if slow:
        remote_tags = dict(
            [
                line.split("\t")[::-1]
                for line in repo.git.ls_remote("--tags", "origin").splitlines()
            ]
        )
        issues["tags_local_only"] = [
            tag for tag in local_tags if tag not in remote_tags
        ]
        issues["tags_mismatch"] = [
            tag
            for tag in local_tags
            if tag in remote_tags and remote_tags[tag] != local_tags[tag]
        ]
    issues = {k: v for k, v in issues.items() if v}
    return issues


def issues_for_one_folder(folder: Path, slow: bool) -> dict[str, Any]:
    try:
        repo = Repo(folder.resolve())
    except InvalidGitRepositoryError:
        return {"is_git": False}
    try:
        repo_st = repo_issues_in_stats(repo, slow)
        branches_st = repo_issues_in_branches(repo, slow)
        tags_st = repo_issues_in_tags(repo, slow)
        submodules_st = {
            f"/{submodule.path}": issues_for_one_folder(Path(submodule.abspath), slow)
            for submodule in repo.submodules
        }
        submodules_st = {k: v for k, v in submodules_st.items() if v}
        return repo_st | branches_st | tags_st | submodules_st
    except Exception as e:
        raise RuntimeError(f"Error while analyzing repo in '{folder}'") from e


def _issues_for_all_subfolders(
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    slow: bool = False,
) -> dict[Path, dict[str, Any]]:
    exclude_dirs = exclude_dirs or []
    issues: dict[Path, dict[str, Any]] = {}
    for folder in basedir.glob("*"):
        if folder.name[0] == "." or folder.name in exclude_dirs:
            continue
        try:
            if not folder.is_dir():
                continue
        except OSError:
            issues[folder] = {"broken_link": folder.readlink().as_posix()}
            continue
        summary = issues_for_one_folder(folder, slow)
        if summary.get("is_git", True) or recurse <= 0:
            issues[folder] = summary
        else:
            subfolder_summary = _issues_for_all_subfolders(
                folder, recurse - 1, exclude_dirs, slow
            )
            if any(st.get("is_git", True) for st in subfolder_summary.values()):
                issues.update(subfolder_summary)
                sym_links = [p.name for p in folder.glob("*") if p.is_symlink()]
                untracked_files = [
                    p.name
                    for p in folder.glob("*")
                    if p.is_file() and not p.is_symlink()
                ]
                if untracked_files or sym_links:
                    issues[folder] = {"is_git": False}
                    if untracked_files:
                        issues[folder]["untracked_files"] = shorten_list(
                            untracked_files
                        )
                    if sym_links:
                        issues[folder]["sym_links"] = shorten_list(sym_links)

            else:
                issues[folder] = {"is_git": False}
    return issues


def issues_for_all_subfolders(
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    slow: bool = False,
) -> dict[str, dict[str, Any]]:
    basedir = Path(basedir)
    # if we are in a git repo, we only check this repo:
    try:
        repo = Repo(basedir, search_parent_directories=True)
    except InvalidGitRepositoryError:
        pass
    else:
        basedir_working_dir = Path(repo.working_tree_dir)
        try:
            from_basedir = basedir_working_dir.relative_to(
                basedir.resolve(), walk_up=True
            ).as_posix()
        except TypeError:
            # walk_up is not supported in python < 3.12
            from_basedir = "<this repos>"
        return {from_basedir: issues_for_one_folder(basedir_working_dir, slow)}

    # otherwise we check all subfolders:
    issues_by_path = _issues_for_all_subfolders(basedir, recurse, exclude_dirs, slow)
    issues = {k.relative_to(basedir).as_posix(): v for k, v in issues_by_path.items()}
    # and we check the basedir itself:
    basedir_files = [p.name for p in basedir.glob("*") if is_file(p)]
    if basedir_files:
        issues["."] = {"untracked_files": shorten_list(basedir_files)}
    return issues


def is_file(p: Path):
    try:
        return p.is_file()
    except OSError:
        # broken link, was reported earlier
        return False


REPORT_FORMATS = ["yaml", "report", "json", "pprint"]
REPORT_FORMATS_TYPE = Literal["yaml", "report", "json", "pprint"]


def format_report(
    issues: dict, *, include_ok: bool, fmt: REPORT_FORMATS_TYPE
) -> str | None:
    if not include_ok:
        issues = {k: v for k, v in issues.items() if v}
    if fmt == "yaml":
        import yaml

        return yaml.dump(
            issues,
            allow_unicode=True,
            default_flow_style=False,
            indent=2,
            sort_keys=False,
        )
    if fmt == "report":
        if not issues:
            return None
        import yaml

        report = yaml.dump(
            issues,
            allow_unicode=True,
            default_flow_style=False,
            indent=2,
            sort_keys=False,
        )
        return "\033[31m" + report + "\033[0m"
    if fmt == "json":
        import json

        return json.dumps(issues, indent=2)
    if fmt == "pprint":
        import pprint

        return pprint.pformat(issues, sort_dicts=False)
    raise ValueError(f"format_report got an unsupported {fmt=}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="git-folder-status", description="find all unpushed data in a directory"
    )
    parser.add_argument("DIRECTORY", help="directory to check", default=".", nargs="?")
    parser.add_argument(
        "-r", "--recurse", type=int, default=3, help="max recurse in directories"
    )
    parser.add_argument(
        "-d", "--exclude-dir", action="append", help="don't include these dirs"
    )
    parser.add_argument(
        "-f",
        "--format",
        default="pprint",
        choices=REPORT_FORMATS,
        help="output format",
    )
    parser.add_argument(
        "-k", "--include-ok", action="store_true", help="show also repos without issues"
    )
    parser.add_argument(
        "-s", "--slow", action="store_true", help="allow slow operations"
    )
    args = parser.parse_args()
    basedir = args.DIRECTORY
    issues = issues_for_all_subfolders(
        basedir, args.recurse, args.exclude_dir, args.slow
    )
    report = format_report(issues, include_ok=args.include_ok, fmt=args.format)
    if report is not None:
        print(report)


if __name__ == "__main__":
    main()
