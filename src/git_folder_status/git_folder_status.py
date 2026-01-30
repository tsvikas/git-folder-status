"""Find all subdirectories with uncommitted or unpushed code.

This code scans through a directory recursively to identify the status of
all Git repositories found within.

Run `git-folder-status -h` for help.
"""

import sys
from collections import ChainMap
from pathlib import Path

from git import InvalidGitRepositoryError, Repo
from git.refs.head import Head


def shorten_list(items: list[str], limit: int = 10) -> list[str]:
    """Truncate a list of strings from the middle."""
    if len(items) <= limit:
        return items
    short_list = items[: limit // 2] + items[-limit // 2 :]
    short_list[limit // 2] = f"<< {len(items) - limit + 1} more items >>"
    return short_list


RepoStats = dict[
    str, "None | str | int | bool | list[str] | RepoStats | list[RepoStats]"
]


def repo_stats(repo: Repo) -> RepoStats:
    """Return stats for a repo."""
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


def repo_issues_in_stats(
    repo: Repo,
    *,
    slow: bool,  # noqa: ARG001
    include_all: bool,
) -> RepoStats:
    """Return issues in a repo."""
    stats_to_include = {
        "is_dirty",
        "untracked_files",
        "stash_count",
        "is_detached_head",
    }
    issues = repo_stats(repo)
    if not include_all:
        issues = {k: v for k, v in issues.items() if k in stats_to_include}
    issues = {k: v for k, v in issues.items() if v}
    return issues


def branch_status(repo: Repo, branch: Head) -> RepoStats:
    """Return stats for a branch."""
    tracking_branch = branch.tracking_branch()
    if tracking_branch is None:
        return {"remote_branch": False}
    local_branch = branch.name
    remote_branch = tracking_branch.name
    if remote_branch[0] == ".":
        # tracking a local branch
        return {"remote_branch": False}
    if remote_branch not in repo.refs:
        return {"remote_branch": remote_branch, "remote_branch_exists": False}
    commits_behind = repo.iter_commits(f"{local_branch}..{remote_branch}")
    commits_ahead = repo.iter_commits(f"{remote_branch}..{local_branch}")
    return {
        "remote_branch": remote_branch,
        "commits_behind": len(list(commits_behind)),
        "commits_ahead": len(list(commits_ahead)),
    }


def all_branches_status(repo: Repo) -> dict[str, RepoStats]:
    """Return stats for all branches in a repo."""
    branches = [b for b in repo.branches if not b.name.startswith("gitbutler/")]
    return {branch.name: branch_status(repo, branch) for branch in branches}


def repo_issues_in_branches(
    repo: Repo,
    *,
    slow: bool,  # noqa: ARG001
    include_all: bool,
    include_behind: bool,
) -> RepoStats:
    """Return issues for all branches in a repo."""
    branches_st = all_branches_status(repo)
    issues: RepoStats = {}
    issues["branches_without_remote"] = [
        k for k, v in branches_st.items() if not v.get("remote_branch", False)
    ]
    issues["branches_with_missing_remote"] = {
        k: v["remote_branch"]
        for k, v in branches_st.items()
        if v["remote_branch"] and not v.get("remote_branch_exists", True)
    }
    issues["branches_out_of_sync"] = {
        k: v
        for k, v in branches_st.items()
        if v["remote_branch"]
        and v.get("remote_branch_exists", True)
        and (
            (v["commits_behind"] or v["commits_ahead"])
            if include_behind
            else v["commits_ahead"]
        )
    }
    if include_all:
        issues["branches"] = {
            k: {kk: vv for kk, vv in v.items() if vv}
            for k, v in branches_st.items()
            if v["remote_branch"]
            and v.get("remote_branch_exists", True)
            and not (v["commits_behind"] or v["commits_ahead"])
        }
    issues = {k: v for k, v in issues.items() if v}
    return issues


def repo_issues_in_tags(repo: Repo, *, slow: bool, include_all: bool) -> RepoStats:
    """Return issues for all tags in a repo."""
    issues: RepoStats = {}
    local_tags: dict[str, str] = {tag.path: tag.commit.hexsha for tag in repo.tags}
    if include_all:
        issues["local_tags"] = local_tags  # type: ignore[assignment]
    if slow:
        remote_tags: ChainMap[str, str] = ChainMap(
            *(
                dict(
                    [
                        line.split("\t")[::-1]
                        for line in repo.git.ls_remote(
                            "--tags", remote_name
                        ).splitlines()
                    ]
                )
                for remote_name in repo.remotes
            )
        )
        remote_tags2: dict[str, str] = {
            k.removesuffix("^{}"): v
            for k, v in remote_tags.items()
            if k.endswith("^{}")
        }
        remote_tags3: ChainMap[str, str] = remote_tags | remote_tags2
        issues["tags_local_only"] = [
            tag for tag in local_tags if tag not in remote_tags3
        ]
        issues["tags_mismatch"] = [  # type: ignore[assignment]
            {tag: {"local": local_tags[tag], "remote": remote_tags3[tag]}}
            for tag in local_tags
            if tag in remote_tags3 and remote_tags3[tag] != local_tags[tag]
        ]
    issues = {k: v for k, v in issues.items() if v}
    return issues


def _filter_submodule_issues(issues: RepoStats) -> RepoStats:
    """Filter issues that aren't relevant for submodules.

    Submodules are pinned to specific commits, so being "behind" the remote
    is expected. We only care about branches that are ahead (unpushed commits).
    """
    filtered = {k: v for k, v in issues.items() if k != "is_detached_head"}
    if "branches_out_of_sync" in filtered:
        branches = filtered["branches_out_of_sync"]
        assert isinstance(branches, dict)  # noqa: S101
        # Only keep branches that have commits ahead (unpushed local commits)
        filtered["branches_out_of_sync"] = {
            branch: status
            for branch, status in branches.items()
            if isinstance(status, dict)
            and isinstance(commits_ahead := status.get("commits_ahead"), int)
            and commits_ahead > 0
        }
        if not filtered["branches_out_of_sync"]:
            del filtered["branches_out_of_sync"]
    return filtered


def issues_for_one_folder(
    folder: Path, *, slow: bool, include_all: bool, include_behind: bool
) -> RepoStats:
    """Return issues for a repos in a folder."""
    try:
        with Repo(
            folder.resolve(), search_parent_directories=folder.is_symlink()
        ) as repo:
            repo_st = repo_issues_in_stats(repo, slow=slow, include_all=include_all)
            branches_st = repo_issues_in_branches(
                repo,
                slow=slow,
                include_all=include_all,
                include_behind=include_behind,
            )
            tags_st = repo_issues_in_tags(repo, slow=slow, include_all=include_all)
            submodules_st = {
                f"/{submodule.path}": _filter_submodule_issues(
                    issues_for_one_folder(
                        Path(submodule.abspath),
                        slow=slow,
                        include_all=include_all,
                        include_behind=include_behind,
                    )
                )
                for submodule in repo.submodules
            }
        submodules_st = {k: v for k, v in submodules_st.items() if v}
        assert isinstance(repo_st, dict)  # noqa: S101
        assert isinstance(branches_st, dict)  # noqa: S101
        assert isinstance(tags_st, dict)  # noqa: S101
        assert isinstance(submodules_st, dict)  # noqa: S101
        issues: RepoStats = repo_st | branches_st | tags_st | submodules_st  # type: ignore[operator]
    except InvalidGitRepositoryError:
        return {"is_git": False} if any(folder.glob("*")) else {}
    except Exception as e:
        raise RuntimeError(f"Error while analyzing repo in '{folder}'") from e
    else:
        return issues


def _issues_for_all_subfolders(  # noqa: PLR0913
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    slow: bool,
    include_all: bool,
    include_behind: bool,
) -> dict[Path, RepoStats]:
    exclude_dirs = exclude_dirs or []
    issues: dict[Path, RepoStats] = {}
    for folder in basedir.glob("*"):
        if folder.name[0] == "." or folder.name in exclude_dirs:
            continue
        if folder.is_symlink():
            if not folder.resolve().exists():
                issues[folder] = {"broken_link": folder.readlink().as_posix()}
                continue
            if basedir in folder.resolve().parents:
                continue
        if not folder.is_dir():
            continue
        summary = issues_for_one_folder(
            folder,
            slow=slow,
            include_all=include_all,
            include_behind=include_behind,
        )
        if summary.get("is_git", True) or recurse <= 0:
            issues[folder] = summary
        else:
            subfolder_summary = _issues_for_all_subfolders(
                folder,
                recurse - 1,
                exclude_dirs,
                slow=slow,
                include_all=include_all,
                include_behind=include_behind,
            )
            if any(st.get("is_git", True) for st in subfolder_summary.values()):
                issues.update(subfolder_summary)
                untracked_files = [p.name for p in folder.glob("*") if p.is_file()]
                if untracked_files:
                    issues[folder] = {"is_git": False}
                    issues[folder]["untracked_files"] = shorten_list(untracked_files)
            else:
                issues[folder] = {"is_git": False}
    return issues


def issues_for_all_subfolders(  # noqa: PLR0913
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    slow: bool = False,
    include_all: bool = False,
    include_behind: bool = False,
) -> dict[str, RepoStats]:
    """Return issues for all repos in a folder."""
    basedir = Path(basedir)
    # if we are in a git repo, we only check this repo:
    try:
        with Repo(basedir, search_parent_directories=True) as repo:
            working_tree_dir = repo.working_tree_dir
        assert working_tree_dir is not None  # noqa: S101
        basedir_working_dir = Path(working_tree_dir)
        if sys.version_info >= (3, 12):
            # pylint: disable=unexpected-keyword-arg
            from_basedir = basedir_working_dir.relative_to(
                basedir.resolve(), walk_up=True
            ).as_posix()
        else:
            # walk_up is not supported in python < 3.12
            from_basedir = "<this repos>"
        return {
            from_basedir: issues_for_one_folder(
                basedir_working_dir,
                slow=slow,
                include_all=include_all,
                include_behind=include_behind,
            )
        }
    except InvalidGitRepositoryError:
        pass

    # otherwise we check all subfolders:
    issues_by_path = _issues_for_all_subfolders(
        basedir,
        recurse,
        exclude_dirs,
        slow=slow,
        include_all=include_all,
        include_behind=include_behind,
    )
    issues = {k.relative_to(basedir).as_posix(): v for k, v in issues_by_path.items()}
    # and we check the basedir itself:
    basedir_files = [p.name for p in basedir.glob("*") if is_file(p)]
    if basedir_files:
        issues["."] = {"untracked_files": shorten_list(basedir_files)}
    return issues


def is_file(p: Path) -> bool:
    """Check if a path is a file."""
    try:
        return p.is_file()
    except OSError:
        # broken link, was reported earlier
        return False
