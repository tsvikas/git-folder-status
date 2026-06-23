"""Find all subdirectories with uncommitted or unpushed code.

This code scans through a directory recursively to identify the status of
all Git repositories found within.

Run `git-folder-status -h` for help.
"""

import sys
from collections import ChainMap
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo
from git.refs.head import Head
from git.refs.remote import RemoteReference

IGNORED_FILENAMES = [".DS_Store"]


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

# Stats that live in the shared object/ref store and are therefore identical
# across every worktree of a repo. Everything else (dirty state, untracked
# files, detached HEAD, submodules) is specific to one working tree.
SHARED_REPO_KEYS = frozenset(
    {
        "stash_count",
        "remotes",
        "branches",
        "branches_local_only",
        "branches_upstream_unset",
        "branches_upstream_gone",
        "branches_out_of_sync",
        "local_tags",
        "tags_local_only",
        "tags_mismatch",
    }
)


@dataclass(frozen=True)
class RepoIdentity:
    """Identifies the repo a scanned folder belongs to.

    `common_dir` is git's shared git dir (`--git-common-dir`): all worktrees of
    one repo share it, so it is the key used to group them. `git_dir` is this
    worktree's own git dir, which differs from `common_dir` only for a linked
    worktree.
    """

    common_dir: Path
    git_dir: Path

    @property
    def is_linked_worktree(self) -> bool:
        """Whether this folder is a linked worktree rather than the main one."""
        return self.common_dir != self.git_dir


def repo_stats(repo: Repo) -> RepoStats:
    """Return stats for a repo."""
    # `git status` and `git stash` fail without a work tree, so skip them
    # in bare repos; the ref-based stats below work fine
    bare = repo.bare
    untracked_files = [] if bare else shorten_list(repo.untracked_files)
    head = repo.head
    try:
        commit = head.commit
    except ValueError:
        # handle an empty repo
        commit = None
    return {
        "bare": bare,
        "is_dirty": repo.is_dirty(),
        "untracked_files": untracked_files,
        "is_detached_head": head.is_detached,
        "active_branch": None if head.is_detached else repo.active_branch.name,
        "head_commit_hash_short": commit.hexsha[:7] if commit else None,
        "branches": {b.name: b.commit.hexsha for b in repo.branches},
        "remotes": {r.name: list(r.urls) for r in repo.remotes},
        "stash_count": 0 if bare else len(repo.git.stash("list").splitlines()),
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


def _matching_remote_branch(repo: Repo, branch: Head) -> RemoteReference | None:
    """Find a remote branch that matches `branch` by name.

    Catches branches that were pushed without `-u`:
    the remote branch exists, but no upstream is configured.
    Matches by name suffix, so `origin/user/feature` matches a local `feature`.
    """
    suffix = f"/{branch.name}"
    candidates = [
        ref
        for remote in repo.remotes
        for ref in remote.refs
        if ref.name.endswith(suffix)
    ]
    if not candidates:
        return None
    # prefer the conventional `<remote>/<branch>` name over prefixed variants
    exact_names = {f"{remote.name}{suffix}" for remote in repo.remotes}
    exact = [ref for ref in candidates if ref.name in exact_names]
    return (exact or candidates)[0]


def branch_status(repo: Repo, branch: Head) -> RepoStats:
    """Return stats for a branch."""
    tracking_branch = branch.tracking_branch()
    # a tracking name starting with "." means tracking a local branch
    if tracking_branch is None or tracking_branch.name[0] == ".":
        # no upstream configured, but the branch may still exist on a remote
        matching = _matching_remote_branch(repo, branch)
        if matching is None:
            return {"remote_branch": False}
        commits_behind = repo.iter_commits(f"{branch.name}..{matching.name}")
        commits_ahead = repo.iter_commits(f"{matching.name}..{branch.name}")
        return {
            "remote_branch": False,
            "matching_remote_branch": matching.name,
            "commits_behind": len(list(commits_behind)),
            "commits_ahead": len(list(commits_ahead)),
        }
    local_branch = branch.name
    remote_branch = tracking_branch.name
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
    issues["branches_local_only"] = [
        k
        for k, v in branches_st.items()
        if not v.get("remote_branch", False) and not v.get("matching_remote_branch")
    ]
    # pushed without `-u`: the remote branch exists but no upstream is set
    issues["branches_upstream_unset"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "remote_branch" and vv}
        for k, v in branches_st.items()
        if not v.get("remote_branch", False) and v.get("matching_remote_branch")
    }
    # upstream is configured but its ref no longer exists (git's "gone" state)
    issues["branches_upstream_gone"] = {
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


def is_orphaned_worktree(folder: Path) -> bool:
    """Check if folder is an orphaned git worktree.

    A worktree has a `.git` file (not directory) pointing to a gitdir.
    If that gitdir no longer exists, the worktree is orphaned.
    """
    git_path = folder / ".git"
    if not git_path.is_file():
        return False
    text = git_path.read_text().strip()
    if not text.startswith("gitdir:"):
        return False
    gitdir = Path(text.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = (folder / gitdir).resolve()
    return not gitdir.is_dir()


def issues_for_one_folder(
    folder: Path, *, slow: bool, include_all: bool, include_behind: bool
) -> tuple[RepoStats, RepoIdentity | None]:
    """Return issues for a repo in a folder, plus its repo identity.

    The identity is `None` when the folder is not a (readable) git repo. It is
    used by the caller to group worktrees of the same repo together.
    """
    try:
        with Repo(
            folder.resolve(), search_parent_directories=folder.is_symlink()
        ) as repo:
            identity = RepoIdentity(
                common_dir=Path(repo.common_dir).resolve(),
                git_dir=Path(repo.git_dir).resolve(),
            )
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
                    )[0]
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
        return ({"is_git": False} if any(folder.glob("*")) else {}), None
    except GitCommandError as e:
        if is_orphaned_worktree(folder):
            return {"error": "orphaned worktree"}, None
        stderr = (e.stderr or "").strip().strip("'\"") or str(e)
        return {"error": f"git error: {stderr}"}, None
    except Exception as e:
        raise RuntimeError(f"Error while analyzing repo in '{folder}'") from e
    else:
        return issues, identity


def _relative_key(path: Path, basedir: Path) -> str:
    """Format `path` as a key relative to `basedir`.

    Falls back to a `../`-relative path (Python 3.12+) and finally an absolute
    path for worktrees that live outside the scanned base directory.
    """
    try:
        return path.relative_to(basedir).as_posix()
    except ValueError:
        pass
    base, abs_path = basedir.resolve(), path.resolve()
    if sys.version_info >= (3, 12):
        # pylint: disable=unexpected-keyword-arg
        try:
            return abs_path.relative_to(base, walk_up=True).as_posix()
        except ValueError:
            pass
    return abs_path.as_posix()


def _split_shared_stats(stats: RepoStats) -> tuple[RepoStats, RepoStats]:
    """Split stats into (shared repo-level, this-worktree-only) parts."""
    shared = {k: v for k, v in stats.items() if k in SHARED_REPO_KEYS}
    local = {k: v for k, v in stats.items() if k not in SHARED_REPO_KEYS}
    return shared, local


def _main_worktree_dir(common_dir: Path) -> Path:
    """Derive the main worktree path from a repo's shared git dir.

    For a normal repo the shared git dir is `<repo>/.git`, so the main worktree
    is its parent. For a bare repo the shared git dir is the repo itself.
    """
    return common_dir.parent if common_dir.name == ".git" else common_dir


def _group_worktrees(
    issues_by_path: dict[Path, RepoStats],
    identities: dict[Path, RepoIdentity],
    basedir: Path,
) -> dict[str, RepoStats]:
    """Group linked worktrees of one repo under their main worktree.

    All worktrees of a repo share one object/ref store, so repo-level state
    (stash, branches, tags, remotes) is identical across them. That shared
    state is reported once on the main worktree; each linked worktree's
    working-tree-specific state is nested under a `worktrees` key. Folders that
    are not git repos, and repos with no linked worktree in the scan, pass
    through unchanged.
    """
    groups: dict[Path, list[Path]] = {}
    for folder, identity in identities.items():
        groups.setdefault(identity.common_dir, []).append(folder)

    grouped: dict[str, RepoStats] = {}
    for folder, stats in issues_by_path.items():
        if folder not in identities:
            grouped[_relative_key(folder, basedir)] = stats

    for common_dir, folders in groups.items():
        linked = [f for f in folders if identities[f].is_linked_worktree]
        mains = [f for f in folders if not identities[f].is_linked_worktree]
        if not linked:
            for folder in folders:
                grouped[_relative_key(folder, basedir)] = issues_by_path[folder]
            continue
        if mains:
            main_folder = mains[0]
            entry = dict(issues_by_path[main_folder])
        else:
            # the main worktree was not scanned: host the shared state here
            main_folder = _main_worktree_dir(common_dir)
            shared, _ = _split_shared_stats(issues_by_path[linked[0]])
            entry = dict(shared)
            entry["main_worktree_unscanned"] = True
        worktrees = {
            _relative_key(folder, basedir): _split_shared_stats(issues_by_path[folder])[
                1
            ]
            for folder in linked
        }
        entry["worktrees"] = {k: worktrees[k] for k in sorted(worktrees)}
        grouped[_relative_key(main_folder, basedir)] = entry
    return grouped


def _list_worktree_paths(repo: Repo) -> list[Path]:
    """Return the working-tree paths of every worktree of `repo`.

    Parses `git worktree list --porcelain`. Bare entries have no working tree
    and prunable entries point at a missing directory, so both are skipped.
    """
    out = repo.git.worktree("list", "--porcelain")
    paths: list[Path] = []
    current: Path | None = None
    skip = False
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
            skip = False
        elif line == "bare" or line.startswith("prunable"):
            skip = True
        elif line == "" and current is not None:
            if not skip:
                paths.append(current)
            current, skip = None, False
    return paths


def _discover_external_worktrees(
    issues_by_path: dict[Path, RepoStats],
    identities: dict[Path, RepoIdentity],
    *,
    slow: bool,
    include_all: bool,
    include_behind: bool,
) -> None:
    """Analyze worktrees that live outside the scanned tree, in place.

    All worktrees of a repo share one worktree list, so for each repo found in
    the scan we enumerate its worktrees and analyze any that were not already
    scanned, wherever they live. `issues_by_path` and `identities` are mutated.
    """
    scanned = {path.resolve() for path in identities}
    seen_common_dirs: set[Path] = set()
    for folder, identity in list(identities.items()):
        if identity.common_dir in seen_common_dirs:
            continue
        seen_common_dirs.add(identity.common_dir)
        try:
            with Repo(folder.resolve()) as repo:
                worktree_paths = _list_worktree_paths(repo)
        except (InvalidGitRepositoryError, GitCommandError):
            continue
        for wt_path in worktree_paths:
            if wt_path.resolve() in scanned or not wt_path.is_dir():
                continue
            stats, wt_identity = issues_for_one_folder(
                wt_path,
                slow=slow,
                include_all=include_all,
                include_behind=include_behind,
            )
            if wt_identity is None:
                continue
            issues_by_path[wt_path] = stats
            identities[wt_path] = wt_identity
            scanned.add(wt_path.resolve())


def _issues_for_all_subfolders(  # noqa: PLR0913
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    slow: bool,
    include_all: bool,
    include_behind: bool,
    identities: dict[Path, RepoIdentity],
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
        summary, identity = issues_for_one_folder(
            folder,
            slow=slow,
            include_all=include_all,
            include_behind=include_behind,
        )
        if summary.get("is_git", True) or recurse <= 0:
            issues[folder] = summary
            if identity is not None:
                identities[folder] = identity
        else:
            issues.update(
                _scan_nested_repos(
                    folder,
                    recurse,
                    exclude_dirs,
                    slow=slow,
                    include_all=include_all,
                    include_behind=include_behind,
                    identities=identities,
                )
            )
    return issues


def _scan_nested_repos(  # noqa: PLR0913
    folder: Path,
    recurse: int,
    exclude_dirs: list[str],
    *,
    slow: bool,
    include_all: bool,
    include_behind: bool,
    identities: dict[Path, RepoIdentity],
) -> dict[Path, RepoStats]:
    """Recurse into a non-repo folder and summarize the repos beneath it."""
    subfolder_summary = _issues_for_all_subfolders(
        folder,
        recurse - 1,
        exclude_dirs,
        slow=slow,
        include_all=include_all,
        include_behind=include_behind,
        identities=identities,
    )
    if not any(st.get("is_git", True) for st in subfolder_summary.values()):
        return {folder: {"is_git": False}}
    result: dict[Path, RepoStats] = dict(subfolder_summary)
    untracked_files = [
        p.name
        for p in folder.glob("*")
        if p.is_file() and p.name not in IGNORED_FILENAMES
    ]
    if untracked_files:
        result[folder] = {
            "is_git": False,
            "untracked_files": shorten_list(untracked_files),
        }
    return result


def issues_for_all_subfolders(  # noqa: PLR0913
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    slow: bool = False,
    include_all: bool = False,
    include_behind: bool = False,
    scan_external_worktrees: bool = False,
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
            )[0]
        }
    except InvalidGitRepositoryError:
        pass

    # otherwise we check all subfolders:
    identities: dict[Path, RepoIdentity] = {}
    issues_by_path = _issues_for_all_subfolders(
        basedir,
        recurse,
        exclude_dirs,
        slow=slow,
        include_all=include_all,
        include_behind=include_behind,
        identities=identities,
    )
    if scan_external_worktrees:
        _discover_external_worktrees(
            issues_by_path,
            identities,
            slow=slow,
            include_all=include_all,
            include_behind=include_behind,
        )
    issues = _group_worktrees(issues_by_path, identities, basedir)
    # and we check the basedir itself:
    basedir_files = [
        p.name
        for p in basedir.glob("*")
        if is_file(p) and p.name not in IGNORED_FILENAMES
    ]
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
