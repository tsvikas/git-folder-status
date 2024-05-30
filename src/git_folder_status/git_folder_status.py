#!/usr/bin/env python3
import argparse
from pathlib import Path
from typing import Any

from git import InvalidGitRepositoryError, Repo
from git.refs.head import Head
from git.remote import FetchInfo


def get_git_repo(folder: Path, *, search_parents=False) -> Repo | None:
    try:
        return Repo(folder, search_parent_directories=search_parents)
    except InvalidGitRepositoryError:
        return None


def fetch_remotes(
    repo: Repo, include: list[str] | None = None, exclude: list[str] | None = None
) -> dict[str : list[FetchInfo]]:
    remotes = list(repo.remotes)
    remotes = (
        remotes if (include is None) else [r for r in remotes if r.name in include]
    )
    remotes = (
        remotes if (exclude is None) else [r for r in remotes if r.name not in exclude]
    )
    return {remote.name: remote.fetch() for remote in remotes}


def _all_fetch_remotes(
    basedir: Path,
    recurse: int = 3,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
) -> list[Path]:
    basedir = Path(basedir)
    if repo := get_git_repo(basedir):
        fetch_remotes(repo, include, exclude)
        return [basedir]
    if recurse == 0:
        return []

    exclude_dirs = exclude_dirs or []
    fetched = []
    for folder in basedir.glob("*"):
        if not folder.is_dir() or folder.name[0] == "." or folder.name in exclude_dirs:
            continue
        fetched.extend(_all_fetch_remotes(folder, recurse - 1, include, exclude))
    return fetched


def all_fetch_remotes(
    basedir: Path,
    recurse: int = 3,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
) -> list[Path]:
    fetched = _all_fetch_remotes(basedir, recurse, include, exclude, exclude_dirs)
    return [p.relative_to(basedir).as_posix() for p in fetched]


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
        "active_branch": repo.active_branch.name,
        # TODO: add head if in detached mode
        # 'head': repo.head.commit.hexsha,
        "branches": {b.name: b.commit.hexsha for b in repo.branches},
        "remotes": {r.name: list(r.urls) for r in repo.remotes},
    }


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


def repo_issues(folder: Path, verbose: bool) -> dict[str, Any]:
    try:
        repo = Repo(folder)
    except InvalidGitRepositoryError:
        return {"is_git": False}
    if verbose:
        print(folder.name)
    repo_st = {
        k: v
        for k, v in repo_stats(repo).items()
        if k in {"is_dirty", "untracked_files"} and v
    }
    branches_st = all_branches_status(repo)
    branches_without_remote = [
        k for k, v in branches_st.items() if not v["remote_branch"]
    ]
    branches_out_of_sync = {
        k: v
        for k, v in branches_st.items()
        if v["remote_branch"] and (v["commits_behind"] or v["commits_ahead"])
    }
    if branches_without_remote:
        repo_st["branches_without_remote"] = branches_without_remote
    if branches_out_of_sync:
        repo_st["branches_out_of_sync"] = branches_out_of_sync
    return repo_st


def _all_repos_issues(
    basedir: Path, recurse: int, verbose: bool, exclude_dirs: list[str] or None = None
) -> dict[Path, dict[str, Any]]:
    exclude_dirs = exclude_dirs or []
    issues = {}
    for folder in basedir.glob("*"):
        if not folder.is_dir() or folder.name[0] == "." or folder.name in exclude_dirs:
            continue
        summary = repo_issues(folder, verbose)
        if summary.get("is_git", True) or recurse <= 0:
            issues[folder] = summary
        else:
            subfolder_summary = _all_repos_issues(
                folder, recurse - 1, verbose, exclude_dirs
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


def all_repos_issues(
    basedir: Path, recurse: int, verbose: bool, exclude_dirs: list[str] or None = None
) -> dict[str, dict[str, Any]]:
    basedir = Path(basedir)
    if repo := get_git_repo(basedir, search_parents=True):
        basedir_working_dir = Path(repo.working_tree_dir)
        from_basedir = basedir_working_dir.relative_to(basedir.resolve(), walk_up=True)
        return {from_basedir.as_posix(): repo_issues(basedir_working_dir, verbose)}
    issues = _all_repos_issues(basedir, recurse, verbose, exclude_dirs)
    issues = {k.relative_to(basedir).as_posix(): v for k, v in issues.items()}
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
        prog="git-state", description="find all unpushed data in a directory"
    )
    parser.add_argument("DIRECTORY", help="directory to check")
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
        "-l", "--include_ok", action="store_true", help="show also repos without issues"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="don't show progress"
    )
    args = parser.parse_args()
    verbose = not args.quiet
    basedir = args.DIRECTORY
    if args.fetch:
        all_fetch_remotes(
            basedir,
            args.recurse,
            include=args.include_remote,
            exclude=args.exclude_remote,
            exclude_dirs=args.exclude_dir,
        )
    issues = all_repos_issues(basedir, args.recurse, verbose, args.exclude_dir)
    if verbose:
        print()
    print(format_report(issues, include_ok=args.include_ok, fmt=args.format))


if __name__ == "__main__":
    main()
