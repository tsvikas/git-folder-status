#!/usr/bin/env python3

# Find all subdirectories with uncommitted or unpushed code.
#
# This script scans through a directory recursively to identify the status of
# all Git repositories found within.
#
# It returns a list of repos with teir issues, including:
# * uncommitted changes
# * untracked files
# * stash entries
# * detached head
# * branches without remote
# * branches with unpushed changes
# * directories with any content that is not part of a repo.
#
# It can also fetch updates for all repos.
#
#
# Run `git-folder-status -h` for help.
# Requires GitPython to be used.

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from textwrap import indent
from typing import Any

from git import InvalidGitRepositoryError, Repo
from git.refs.head import Head
from git.remote import FetchInfo


def get_git_repo(folder: Path, *, search_parents: bool = False) -> Repo | None:
    try:
        return Repo(folder, search_parent_directories=search_parents)
    except InvalidGitRepositoryError:
        return None


async def fetch_remote(remote: Any) -> str:
    """Fetch a single remote asynchronously using a thread pool."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        await loop.run_in_executor(pool, remote.fetch)


async def fetch_single_repo(
    repo: Repo, include: list[str] | None = None, exclude: list[str] | None = None
) -> dict[str, Exception | None]:
    """Fetch all remotes for a single repository asynchronously."""
    remotes = list(repo.remotes)
    if include is not None:
        remotes = [r for r in remotes if r.name in include]
    if exclude is not None:
        remotes = [r for r in remotes if r.name not in exclude]
    fetch_tasks = [fetch_remote(remote) for remote in remotes]
    results = dict(
        zip(
            (remote.name for remote in remotes),
            await asyncio.gather(*fetch_tasks, return_exceptions=True),
        )
    )
    results_OK = {name: exc for name, exc in results.items() if exc is None}
    results_exceptions = {name: exc for name, exc in results.items() if exc is not None}
    return results_OK | results_exceptions


async def fetch_remotes_in_subfolders(
    basedir: Path,
    recurse: int = 3,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    *,
    _recursive_head: bool = True,
) -> dict[tuple[Path, str], Exception | None]:
    basedir = Path(basedir)
    exclude_dirs = exclude_dirs or []

    if repo := get_git_repo(basedir):
        result = await fetch_single_repo(repo, include, exclude)
        return {(basedir, remote): status for remote, status in result.items()}

    if recurse == 0:
        return {}

    folders = [
        folder
        for folder in basedir.glob("*")
        if folder.is_dir()
        and not folder.name.startswith(".")
        and folder.name not in exclude_dirs
    ]
    tasks = [
        fetch_remotes_in_subfolders(
            folder,
            recurse - 1,
            include,
            exclude,
            exclude_dirs=[],
            _recursive_head=False,
        )
        for folder in folders
    ]
    results = await asyncio.gather(*tasks)

    fetched = {}
    for result in results:
        fetched = fetched | result

    if _recursive_head:
        for (p, remote), exc in fetched.items():
            status = "✓" if exc is None else "𐄂"
            print(f"{status} {p.relative_to(basedir).as_posix()}:{remote}")
            if exc is not None:
                print(indent(str(exc), "  "))
    return fetched


def shorten_filelist(filelist: list[str], limit: int = 10) -> list[str]:
    if len(filelist) <= limit:
        return filelist
    short_list = filelist[: limit // 2] + filelist[-limit // 2 :]
    short_list[limit // 2] = f"<< {len(filelist) - limit + 1} more items >>"
    return short_list


def repo_stats(repo: Repo) -> dict[str, Any]:
    untracked_files = shorten_filelist(repo.untracked_files)
    return {
        "is_dirty": repo.is_dirty(),
        "untracked_files": untracked_files,
        "is_detached_head": repo.head.is_detached,
        "active_branch": None if repo.head.is_detached else repo.active_branch.name,
        "head_commit_hash_short": repo.head.commit.hexsha[:7],
        "branches": {b.name: b.commit.hexsha for b in repo.branches},
        "remotes": {r.name: list(r.urls) for r in repo.remotes},
        "stash_count": len(repo.git.stash("list").splitlines()),
    }


def repo_issues_in_stats(repo: Repo) -> dict[str, Any]:
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
        # 'local_branch': local_branch,
        "remote_branch": remote_branch,
        "commits_behind": len(list(commits_behind)),
        "commits_ahead": len(list(commits_ahead)),
    }


def all_branches_status(repo: Repo) -> dict[str, dict[str, Any]]:
    return {branch.name: branch_status(repo, branch) for branch in repo.branches}


def repo_issues_in_branches(repo: Repo) -> dict[str, Any]:
    branches_st = all_branches_status(repo)
    issues = dict()
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


def issues_for_one_folder(folder: Path) -> dict[str, Any]:
    try:
        repo = Repo(folder)
    except InvalidGitRepositoryError:
        return {"is_git": False}
    repo_st = repo_issues_in_stats(repo)
    branches_st = repo_issues_in_branches(repo)
    return repo_st | branches_st


def _issues_for_all_subfolders(
    basedir: Path, recurse: int, exclude_dirs: list[str] or None = None
) -> dict[Path, dict[str, Any]]:
    exclude_dirs = exclude_dirs or []
    issues = {}
    for folder in basedir.glob("*"):
        if not folder.is_dir() or folder.name[0] == "." or folder.name in exclude_dirs:
            continue
        summary = issues_for_one_folder(folder)
        if summary.get("is_git", True) or recurse <= 0:
            issues[folder] = summary
        else:
            subfolder_summary = _issues_for_all_subfolders(
                folder, recurse - 1, exclude_dirs
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
                        issues[folder]["untracked_files"] = shorten_filelist(
                            untracked_files
                        )
                    if sym_links:
                        issues[folder]["sym_links"] = shorten_filelist(sym_links)

            else:
                issues[folder] = {"is_git": False}
    return issues


def issues_for_all_subfolders(
    basedir: Path, recurse: int, exclude_dirs: list[str] or None = None
) -> dict[str, dict[str, Any]]:
    basedir = Path(basedir)
    # if we are in a git repo, we only check this repo:
    if repo := get_git_repo(basedir, search_parents=True):
        basedir_working_dir = Path(repo.working_tree_dir)
        try:
            from_basedir = basedir_working_dir.relative_to(
                basedir.resolve(), walk_up=True
            ).as_posix()
        except TypeError:
            # walk_up is not supported in python < 3.12
            from_basedir = "<this repos>"
        return {from_basedir: issues_for_one_folder(basedir_working_dir)}
    # otherwise we check all subfolders:
    issues = _issues_for_all_subfolders(basedir, recurse, exclude_dirs)
    issues = {k.relative_to(basedir).as_posix(): v for k, v in issues.items()}
    # and we check the basedir itself:
    basedir_files = [p.name for p in basedir.glob("*") if p.is_file()]
    if basedir_files:
        issues["."] = {"untracked_files": shorten_filelist(basedir_files)}
    return issues


def format_report(issues: dict, *, include_ok: bool, fmt: str) -> str:
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
        "--fetch", action="store_true", help="run git fetch on all repos"
    )
    parser.add_argument(
        "-i", "--include-remote", action="append", help="only include these remotes"
    )
    parser.add_argument(
        "-x", "--exclude-remote", action="append", help="don't include these remotes"
    )
    parser.add_argument(
        "-d", "--exclude-dir", action="append", help="don't include these dirs"
    )
    parser.add_argument(
        "-f",
        "--format",
        default="pprint",
        choices=["pprint", "json", "yaml"],
        help="output format",
    )
    parser.add_argument(
        "-k", "--include-ok", action="store_true", help="show also repos without issues"
    )
    args = parser.parse_args()
    basedir = args.DIRECTORY
    if args.fetch:
        asyncio.run(
            fetch_remotes_in_subfolders(
                basedir,
                args.recurse,
                include=args.include_remote,
                exclude=args.exclude_remote,
                exclude_dirs=args.exclude_dir,
            )
        )
        print()
    issues = issues_for_all_subfolders(basedir, args.recurse, args.exclude_dir)
    print(format_report(issues, include_ok=args.include_ok, fmt=args.format))


if __name__ == "__main__":
    main()
