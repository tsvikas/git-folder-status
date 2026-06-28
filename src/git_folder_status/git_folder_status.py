"""Find all subdirectories with uncommitted or unpushed code.

This code scans through a directory recursively to identify the status of
all Git repositories found within.

Run `git-folder-status -h` for help.
"""

import sys
from collections import ChainMap
from collections.abc import Iterator
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


def shorten_dict(items: dict[str, str], limit: int = 10) -> dict[str, str]:
    """Truncate a dict from the middle, mirroring `shorten_list`."""
    if len(items) <= limit:
        return items
    entries = list(items.items())
    kept = entries[: limit // 2] + entries[-limit // 2 :]
    kept[limit // 2] = (f"<< {len(items) - limit + 1} more items >>", "")
    return dict(kept)


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
        "local_tags",
        "tags_local_only",
        "tags_mismatch",
    }
)


@dataclass(frozen=True)
class ScanOptions:
    """Flags controlling how each repo is analyzed, threaded through the scan.

    `slow` allows expensive operations (remote tag comparison). `include_all`
    reports context beyond issues. `include_behind` keeps branches that are
    only behind their upstream.
    """

    slow: bool = False
    include_all: bool = False
    include_behind: bool = False


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
        "remotes": {r.name: list(r.urls) for r in repo.remotes},
        "stash_count": 0 if bare else len(repo.git.stash("list").splitlines()),
    }


def repo_issues_in_stats(repo: Repo, options: ScanOptions) -> RepoStats:
    """Return issues in a repo."""
    stats_to_include = {
        "is_dirty",
        "untracked_files",
        "stash_count",
        "is_detached_head",
    }
    issues = repo_stats(repo)
    if not options.include_all:
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


def _ahead_behind(repo: Repo, local: str, remote: str) -> tuple[int, int]:
    """Return (ahead, behind): commits on `local` not on `remote`, and vice versa."""
    behind = len(list(repo.iter_commits(f"{local}..{remote}")))
    ahead = len(list(repo.iter_commits(f"{remote}..{local}")))
    return ahead, behind


def branch_status(repo: Repo, branch: Head) -> RepoStats:
    """Return the upstream relationship of a single branch.

    The `upstream` field discriminates four mutually exclusive states:

    - `set`: a real upstream is configured and its remote ref exists.
    - `unset`: no upstream configured, but a remote branch matches by name
      (typically pushed without `-u`).
      Its ahead/behind counts are measured against that name-matched
      *candidate*, so they are only a guess.
    - `gone`: an upstream is configured but its remote ref no longer exists.
    - `missing`: local-only, no upstream and no matching remote branch.
    """
    head = branch.commit.hexsha
    tracking_branch = branch.tracking_branch()
    # a tracking name starting with "." means tracking a local branch
    if tracking_branch is None or tracking_branch.name[0] == ".":
        # no upstream configured, but the branch may still exist on a remote
        matching = _matching_remote_branch(repo, branch)
        if matching is None:
            return {"upstream": "missing", "head": head}
        ahead, behind = _ahead_behind(repo, branch.name, matching.name)
        return {
            "upstream": "unset",
            "remote_branch": matching.name,
            "ahead": ahead,
            "behind": behind,
            "head": head,
        }
    remote_branch = tracking_branch.name
    if remote_branch not in repo.refs:
        return {"upstream": "gone", "remote_branch": remote_branch, "head": head}
    ahead, behind = _ahead_behind(repo, branch.name, remote_branch)
    return {
        "upstream": "set",
        "remote_branch": remote_branch,
        "ahead": ahead,
        "behind": behind,
        "head": head,
    }


def _iter_worktrees(repo: Repo) -> Iterator[tuple[Path, str | None]]:
    """Yield `(path, branch)` for each usable worktree of `repo`.

    Parses `git worktree list --porcelain`. `branch` is the checked-out branch
    name, or `None` for a detached HEAD. Bare entries (no working tree) and
    prunable entries (pointing at a missing directory) are skipped. The trailing
    empty string flushes the final record, which has no blank line after it.
    """
    out = repo.git.worktree("list", "--porcelain")
    path: Path | None = None
    branch: str | None = None
    skip = False
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
            branch, skip = None, False
        elif line == "bare" or line.startswith("prunable"):
            skip = True
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "" and path is not None:
            if not skip:
                yield path, branch
            path, branch, skip = None, None, False


def _worktree_branches(repo: Repo) -> dict[str, str]:
    """Map each checked-out branch name to the worktree path that holds it.

    Git allows a branch to be checked out in at most one worktree, so the
    mapping is unambiguous. Detached worktrees have no branch and are omitted.
    """
    return {
        branch: path.resolve().as_posix()
        for path, branch in _iter_worktrees(repo)
        if branch is not None
    }


def all_branches_status(repo: Repo) -> dict[str, RepoStats]:
    """Return the upstream status of all branches in a repo.

    A branch checked out in some *other* worktree carries a `worktree` field
    holding that worktree's path, so a diverged branch can be located. A branch
    checked out in `repo` itself gets no such field: the path would only echo
    the repo being reported on, so it is redundant.
    """
    branches = [b for b in repo.branches if not b.name.startswith("gitbutler/")]
    if not branches:
        return {}
    checkouts = _worktree_branches(repo)
    own = Path(repo.working_dir).resolve().as_posix() if repo.working_dir else None
    result: dict[str, RepoStats] = {}
    for branch in branches:
        status = branch_status(repo, branch)
        worktree = checkouts.get(branch.name)
        if worktree is not None and worktree != own:
            status["worktree"] = worktree
        result[branch.name] = status
    return result


def _branch_has_issue(status: RepoStats, *, include_behind: bool) -> bool:
    """Whether a branch's upstream status is worth reporting."""
    # any non-`set` upstream state (missing / unset / gone) is itself the issue
    if status["upstream"] != "set":
        return True
    if status.get("ahead"):
        return True
    return bool(include_behind and status.get("behind"))


def _branch_record(status: RepoStats, *, include_all: bool) -> RepoStats:
    """Shape a branch's upstream status for the report.

    A clean, in-sync branch yields `{}`, so the report's generic "drop falsy"
    pass removes it with no special case: every key present marks a real
    deviation. The `--all` view re-adds `remote_branch`/`head` so even clean
    branches stay non-empty and are listed.
    """
    upstream = status["upstream"]
    record: RepoStats = {}
    if upstream == "missing":
        record["no_remote"] = True
    elif upstream == "gone":
        record["remote_deleted"] = status["remote_branch"]
    elif upstream == "unset":
        # the candidate is matched by name, not configured, so its ahead/behind
        # are provisional: keep them scoped inside this block, never at the top
        # level where counts mean "vs the real upstream".
        candidate: RepoStats = {"candidate": status["remote_branch"]}
        if status.get("ahead"):
            candidate["ahead"] = status["ahead"]
        if status.get("behind"):
            candidate["behind"] = status["behind"]
        record["remote_not_tracked"] = candidate
    else:  # set: ahead/behind are measured against the real configured upstream
        if status.get("ahead"):
            record["ahead"] = status["ahead"]
        if status.get("behind"):
            record["behind"] = status["behind"]
    # remote_branch and head are context, not issue triggers: attach them only
    # under --all, so they never keep a clean branch in the regular report.
    if include_all:
        if status.get("remote_branch"):
            record["remote_branch"] = status["remote_branch"]
        record["head"] = status.get("head")
    return record


def repo_issues_in_branches(repo: Repo, options: ScanOptions) -> RepoStats:
    """Return per-branch upstream issues, keyed under `branches`."""
    branches_st = all_branches_status(repo)
    branches: RepoStats = {}
    for name, status in branches_st.items():
        if not options.include_all and not _branch_has_issue(
            status, include_behind=options.include_behind
        ):
            continue
        # the predicate above (or include_all) guarantees a non-empty record
        record = _branch_record(status, include_all=options.include_all)
        # name the worktree that has this branch checked out, so an unpushed or
        # diverged branch can be located. Only a reported (non-empty) record
        # reaches here, so a clean in-sync branch is never annotated.
        if status.get("worktree"):
            record["worktree"] = status["worktree"]
        branches[name] = record
    return {"branches": branches} if branches else {}


def repo_issues_in_tags(repo: Repo, options: ScanOptions) -> RepoStats:
    """Return issues for all tags in a repo."""
    issues: RepoStats = {}
    local_tags: dict[str, str] = {tag.path: tag.commit.hexsha for tag in repo.tags}
    if options.include_all:
        issues["local_tags"] = shorten_dict(local_tags)  # type: ignore[assignment]
    if options.slow:
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


def _is_branch_behind_only(record: RepoStats) -> bool:
    """Whether a branch's only divergence is being behind its upstream.

    For submodules such a branch is not worth reporting (see below), whereas
    unpushed (`ahead`) commits and any upstream problem still are.
    """
    issue_keys = {"ahead", "no_remote", "remote_not_tracked", "remote_deleted"}
    return "behind" in record and not (issue_keys & record.keys())


def _filter_submodule_issues(issues: RepoStats) -> RepoStats:
    """Filter issues that aren't relevant for submodules.

    Submodules are pinned to specific commits, so being "behind" the remote
    is expected. We only care about branches that are ahead (unpushed commits).
    """
    filtered = {k: v for k, v in issues.items() if k != "is_detached_head"}
    branches = filtered.get("branches")
    if isinstance(branches, dict):
        kept: RepoStats = {
            branch: record
            for branch, record in branches.items()
            if isinstance(record, dict) and not _is_branch_behind_only(record)
        }
        if kept:
            filtered["branches"] = kept
        else:
            del filtered["branches"]
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
    folder: Path, options: ScanOptions
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
            repo_st = repo_issues_in_stats(repo, options)
            branches_st = repo_issues_in_branches(repo, options)
            tags_st = repo_issues_in_tags(repo, options)
            submodules_st = {
                f"/{submodule.path}": _filter_submodule_issues(
                    issues_for_one_folder(Path(submodule.abspath), options)[0]
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


def _relativize_worktree_paths(stats: RepoStats, basedir: Path) -> None:
    """Rewrite absolute `worktree` paths in `stats` as keys relative to `basedir`.

    `all_branches_status` records each branch's worktree as an absolute path.
    Rewriting it against the scanned base mirrors the keys used for the grouped
    `worktrees` entries, so the two can be cross-referenced. Mutates in place,
    recursing through nested branch / submodule / worktree records.
    """
    for key, value in stats.items():
        if key == "worktree" and isinstance(value, str):
            stats[key] = _relative_key(Path(value), basedir)
        elif isinstance(value, dict):
            _relativize_worktree_paths(value, basedir)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _relativize_worktree_paths(item, basedir)


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
    """Return the working-tree path of every usable worktree of `repo`."""
    return [path for path, _ in _iter_worktrees(repo)]


def _discover_external_worktrees(
    issues_by_path: dict[Path, RepoStats],
    identities: dict[Path, RepoIdentity],
    options: ScanOptions,
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
            stats, wt_identity = issues_for_one_folder(wt_path, options)
            if wt_identity is None:
                continue
            issues_by_path[wt_path] = stats
            identities[wt_path] = wt_identity
            scanned.add(wt_path.resolve())


def _issues_for_all_subfolders(
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    options: ScanOptions,
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
        summary, identity = issues_for_one_folder(folder, options)
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
                    options=options,
                    identities=identities,
                )
            )
    return issues


def _scan_nested_repos(
    folder: Path,
    recurse: int,
    exclude_dirs: list[str],
    *,
    options: ScanOptions,
    identities: dict[Path, RepoIdentity],
) -> dict[Path, RepoStats]:
    """Recurse into a non-repo folder and summarize the repos beneath it."""
    subfolder_summary = _issues_for_all_subfolders(
        folder,
        recurse - 1,
        exclude_dirs,
        options=options,
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
    options = ScanOptions(
        slow=slow, include_all=include_all, include_behind=include_behind
    )
    # if we are in a git repo, we only check this repo:
    try:
        with Repo(basedir, search_parent_directories=True) as repo:
            working_tree_dir = repo.working_tree_dir
    except InvalidGitRepositoryError:
        pass
    else:
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
        single = {from_basedir: issues_for_one_folder(basedir_working_dir, options)[0]}
        for stats in single.values():
            _relativize_worktree_paths(stats, basedir)
        return single

    # otherwise we check all subfolders:
    identities: dict[Path, RepoIdentity] = {}
    issues_by_path = _issues_for_all_subfolders(
        basedir,
        recurse,
        exclude_dirs,
        options=options,
        identities=identities,
    )
    if scan_external_worktrees:
        _discover_external_worktrees(issues_by_path, identities, options)
    issues = _group_worktrees(issues_by_path, identities, basedir)
    for stats in issues.values():
        _relativize_worktree_paths(stats, basedir)
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
