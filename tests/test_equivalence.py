"""The fast git-output readers give the same answers as GitPython's clear APIs.

For speed, several scan steps replaced a clear GitPython call with one git call
whose output is parsed by hand, or with a shortcut that relies on how GitPython
works inside. Each test here builds real repos, asks both the clear version (kept
here as the oracle) and the fast one, and asserts they agree. Drift in git's
output or in GitPython's behavior then fails a test. The few intended
differences are pinned by tests of their own.
"""

import dataclasses
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from git import Actor, GitCommandError, Repo

from git_folder_status.git_folder_status import (
    DEFAULT_BRANCH_NAMES,
    BranchRef,
    RepoStats,
    WorkTreeStatus,
    _ahead_behind,
    _git_error_message,
    _submodules,
    all_branches_status,
    branch_status,
    list_branches,
    work_tree_status,
)

ACTOR = Actor("Test", "test@example.com")


def _commit(repo: Repo, filename: str, content: str = "x\n") -> None:
    assert repo.working_tree_dir is not None
    (Path(repo.working_tree_dir) / filename).write_text(content)
    repo.index.add([filename])
    repo.index.commit(filename, author=ACTOR, committer=ACTOR)


def _init(path: Path) -> Repo:
    repo = Repo.init(path)
    repo.git.symbolic_ref("HEAD", "refs/heads/main")
    _commit(repo, "README.md", "init\n")
    return repo


def _git(repo: Repo, *args: str) -> None:
    """Run a git command that may create commits (stash, merge)."""
    repo.git.execute(["git", "-c", "user.name=T", "-c", "user.email=t@e.com", *args])


def _add_submodule(parent: Repo, source: Path, name: str) -> Path:
    # cloning over the file protocol is refused by default since git 2.38
    parent.git(c="protocol.file.allow=always").submodule("add", str(source), name)
    parent.index.commit("add submodule", author=ACTOR, committer=ACTOR)
    assert parent.working_tree_dir is not None
    return Path(parent.working_tree_dir) / name


# --- work tree state: `git status --porcelain=v2` vs GitPython ---------------


def _gitpython_work_tree(repo: Repo) -> WorkTreeStatus:
    """Read the work tree the clear way: GitPython's own properties."""
    try:
        head: str | None = repo.head.commit.hexsha
    except ValueError:
        head = None  # a repo with no commits
    return WorkTreeStatus(
        head=head,
        branch=None if repo.head.is_detached else repo.active_branch.name,
        is_dirty=repo.is_dirty(),
        untracked_files=sorted(repo.untracked_files),
        stash_count=len(repo.git.stash("list").splitlines()),
    )


def _empty(path: Path) -> Repo:
    repo = Repo.init(path)
    repo.git.symbolic_ref("HEAD", "refs/heads/main")
    return repo


def _modified(path: Path) -> Repo:
    repo = _init(path)
    (path / "README.md").write_text("changed\n")
    return repo


def _staged(path: Path) -> Repo:
    repo = _modified(path)
    repo.index.add(["README.md"])
    return repo


def _deleted(path: Path) -> Repo:
    repo = _init(path)
    (path / "README.md").unlink()
    return repo


def _renamed(path: Path) -> Repo:
    repo = _init(path)
    repo.git.mv("README.md", "new name.md")
    return repo


def _untracked_odd_names(path: Path) -> Repo:
    repo = _init(path)
    (path / "dir" / "sub").mkdir(parents=True)
    (path / "dir" / "sub" / "x y.txt").write_text("1")
    (path / "ünï.txt").write_text("1")
    (path / "#hash.txt").write_text("1")
    return repo


# names git C-quotes outside `-z` output, which GitPython has to unquote
QUOTED_NAMES = ('a"b.txt', "back\\slash.txt", "tab\tname.txt")


def _untracked_quoted_names(path: Path) -> Repo:
    repo = _init(path)
    for name in QUOTED_NAMES:
        (path / name).write_text("1")
    return repo


def _renamed_to_quoted_name(path: Path) -> Repo:
    repo = _init(path)
    repo.git.mv("README.md", QUOTED_NAMES[0])
    return repo


def _ignored(path: Path) -> Repo:
    repo = _init(path)
    (path / ".gitignore").write_text("*.log\n")
    (path / "x.log").write_text("1")
    return repo


def _two_stashes(path: Path) -> Repo:
    repo = _init(path)
    for i in range(2):
        (path / "README.md").write_text(f"{i}\n")
        _git(repo, "stash")
    return repo


def _detached(path: Path) -> Repo:
    repo = _init(path)
    repo.git.checkout("--detach")
    return repo


def _empty_staged(path: Path) -> Repo:
    repo = _empty(path)
    (path / "a.txt").write_text("1")
    repo.index.add(["a.txt"])
    return repo


def _empty_untracked(path: Path) -> Repo:
    repo = _empty(path)
    (path / "a.txt").write_text("1")
    return repo


def _merge_conflict(path: Path) -> Repo:
    repo = _init(path)
    repo.git.switch("-c", "other")
    _commit(repo, "README.md", "other\n")
    repo.git.switch("main")
    _commit(repo, "README.md", "mine\n")
    with pytest.raises(GitCommandError):
        _git(repo, "merge", "other")
    return repo


def _with_submodule(path: Path) -> tuple[Repo, Repo]:
    """Return a parent repo and its checked-out submodule `sm`."""
    _init(path.parent / f"{path.name}-sub").close()
    parent = _init(path)
    checkout = _add_submodule(parent, path.parent / f"{path.name}-sub", "sm")
    return parent, Repo(checkout)


def _submodule_modified(path: Path) -> Repo:
    parent, sub = _with_submodule(path)
    (Path(sub.working_dir) / "README.md").write_text("changed\n")
    sub.close()
    return parent


def _submodule_untracked_only(path: Path) -> Repo:
    parent, sub = _with_submodule(path)
    (Path(sub.working_dir) / "new.txt").write_text("1")
    sub.close()
    return parent


def _submodule_new_commit(path: Path) -> Repo:
    parent, sub = _with_submodule(path)
    _commit(sub, "new.txt")
    sub.close()
    return parent


WORK_TREE_CASES: dict[str, Callable[[Path], Repo]] = {
    "clean": _init,
    "modified": _modified,
    "staged": _staged,
    "deleted": _deleted,
    "renamed": _renamed,
    "untracked odd names": _untracked_odd_names,
    "untracked quoted names": _untracked_quoted_names,
    "renamed to a quoted name": _renamed_to_quoted_name,
    "ignored file": _ignored,
    "two stashes": _two_stashes,
    "detached": _detached,
    "empty repo": _empty,
    "empty repo, staged": _empty_staged,
    "empty repo, untracked": _empty_untracked,
    "merge conflict": _merge_conflict,
    "submodule modified": _submodule_modified,
    "submodule untracked only": _submodule_untracked_only,
    "submodule new commit": _submodule_new_commit,
}


# `"`, `\` and tab are not allowed in Windows file names
_WINDOWS_UNSAFE = {"untracked quoted names", "renamed to a quoted name"}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.skipif(
                case in _WINDOWS_UNSAFE and sys.platform == "win32",
                reason="file name not allowed on Windows",
            ),
        )
        for case in WORK_TREE_CASES
    ],
)
def test_work_tree_status_matches_gitpython(tmp_path: Path, case: str) -> None:
    """One `git status` call reads what GitPython's properties read."""
    with WORK_TREE_CASES[case](tmp_path / "repo") as repo:
        fast = work_tree_status(repo)
        clear = _gitpython_work_tree(repo)
    assert dataclasses.replace(fast, untracked_files=sorted(fast.untracked_files)) == (
        clear
    )


def test_ignored_submodule_changes_differ(tmp_path: Path) -> None:
    """Intended difference: `diff.ignoreSubmodules` does not hide changes.

    GitPython's `is_dirty()` runs `git diff`, which honors the setting, so a
    submodule's uncommitted change reads as clean. The scan overrides it: a
    change inside a submodule is unsaved work, which is always reported.
    """
    with _submodule_modified(tmp_path / "repo") as repo:
        repo.git.config("diff.ignoreSubmodules", "all")
        assert work_tree_status(repo).is_dirty is True
        assert repo.is_dirty() is False


# --- branch upstreams: `for-each-ref` vs GitPython --------------------------

Upstream = RepoStats


def _gitpython_upstream(repo: Repo, name: str) -> Upstream:
    """Read a branch's upstream state the clear way, as the scan did before.

    Uses `tracking_branch()`, a `repo.refs` lookup and `iter_commits` counts.
    Only branches with a configured upstream are modelled; for the others the
    scan matches remote branches by name, which this PR did not change.
    """
    head = repo.heads[name]
    tracking = head.tracking_branch()
    # a tracking name starting with "." means tracking a local branch
    if tracking is None or tracking.name.startswith("."):
        return {"head": head.commit.hexsha, "upstream": None}
    record: Upstream = {
        "head": head.commit.hexsha,
        "remote_branch": tracking.name,
        "remote": tracking.remote_name,
        "remote_head": tracking.remote_head,
    }
    if tracking.name not in repo.refs:
        record["upstream"] = "gone"
    elif tracking.remote_head in DEFAULT_BRANCH_NAMES and (
        name not in DEFAULT_BRANCH_NAMES
    ):
        record["upstream"] = "default"
    else:
        record["upstream"] = "set"
        record["ahead"] = len(list(repo.iter_commits(f"{tracking.name}..{name}")))
        record["behind"] = len(list(repo.iter_commits(f"{name}..{tracking.name}")))
    return record


def _scan_upstream(repo: Repo, branch: BranchRef) -> Upstream:
    """Read the same fields from the scan: `list_branches` + `branch_status`."""
    status = branch_status(repo, branch)
    if not branch.upstream:
        return {"head": status["head"], "upstream": None}
    record: Upstream = {
        "head": status["head"],
        "remote_branch": status["remote_branch"],
        "remote": branch.remote,
        "remote_head": branch.remote_head,
        "upstream": status["upstream"],
    }
    if status["upstream"] == "set":
        record["ahead"] = status["ahead"]
        record["behind"] = status["behind"]
    return record


def _scan_upstreams(repo: Repo) -> dict[str, Upstream]:
    return {b.name: _scan_upstream(repo, b) for b in list_branches(repo)}


def _clone_with_branches(tmp_path: Path) -> Repo:
    """Clone a repo and give it one branch per upstream kind."""
    source = _init(tmp_path / "source")
    source.git.branch("user/feat")
    repo = Repo.clone_from(str(tmp_path / "source"), tmp_path / "clone")
    _commit(source, "upstream.txt")  # origin/main gets a commit main lacks
    repo.git.fetch()
    _commit(repo, "local.txt")  # and main gets one origin/main lacks
    repo.git.branch("--track", "synced", "origin/main")
    repo.git.branch("--track", "user/feat", "origin/user/feat")
    repo.git.push("origin", "main:old")
    repo.git.branch("--track", "old", "origin/old")
    repo.git.push("origin", "--delete", "old")
    repo.git.branch("plain")
    repo.git.branch("--track", "on-local", "plain")
    source.close()
    return repo


def test_upstreams_match_gitpython(tmp_path: Path) -> None:
    """Upstream state, remote, commit and counts agree, branch by branch."""
    with _clone_with_branches(tmp_path) as repo:
        fast = _scan_upstreams(repo)
        clear = {head.name: _gitpython_upstream(repo, head.name) for head in repo.heads}
    assert fast == clear
    # each upstream kind is exercised, not just present
    states = {name: record["upstream"] for name, record in fast.items()}
    assert states == {
        "main": "set",
        "user/feat": "set",
        "synced": "default",
        "old": "gone",
        "plain": None,
        "on-local": None,
    }
    assert (fast["main"]["ahead"], fast["main"]["behind"]) == (1, 1)


def test_upstream_of_removed_remote_differs(tmp_path: Path) -> None:
    """Intended difference: a remote removed from the config leaves no upstream.

    GitPython still compares against the leftover `refs/remotes/<removed>/*`,
    which never update; git, and so the scan, reports no upstream.
    """
    with _clone_with_branches(tmp_path) as repo:
        repo.git.config("--remove-section", "remote.origin")
        assert _scan_upstreams(repo)["main"]["upstream"] is None
        assert _gitpython_upstream(repo, "main")["upstream"] == "set"


def test_upstream_of_slash_remote_differs(tmp_path: Path) -> None:
    """Intended difference: a remote named `team/fork` keeps its full name.

    GitPython ends a remote name at the first `/`, reading `team/fork/main` as
    branch `fork/main` on remote `team`; git, and so the scan, names the remote
    `team/fork` and the branch `main`.
    """
    with _clone_with_branches(tmp_path) as repo:
        repo.git.remote("rename", "origin", "team/fork")
        scan = _scan_upstreams(repo)["main"]
        clear = _gitpython_upstream(repo, "main")
    assert (scan["remote"], scan["remote_head"]) == ("team/fork", "main")
    assert (clear["remote"], clear["remote_head"]) == ("team", "fork/main")


def test_ahead_behind_matches_commit_counts(tmp_path: Path) -> None:
    """`rev-list --left-right --count` counts what GitPython's commit lists hold."""
    with _clone_with_branches(tmp_path) as repo:
        counts = _ahead_behind(repo, "refs/heads/main", "refs/remotes/origin/main")
        ahead = len(list(repo.iter_commits("origin/main..main")))
        behind = len(list(repo.iter_commits("main..origin/main")))
    assert counts == (ahead, behind) == (1, 1)


# --- submodules: the `.gitmodules` shortcut vs GitPython --------------------


def _no_gitmodules(path: Path) -> Repo:
    return _init(path)


def _gitmodules_deleted(path: Path) -> Repo:
    """`.gitmodules` deleted from the work tree, still in HEAD."""
    parent, sub = _with_submodule(path)
    sub.close()
    (path / ".gitmodules").unlink()
    return parent


def _bare_with_submodule(path: Path) -> Repo:
    parent, sub = _with_submodule(path.parent / f"{path.name}-src")
    sub.close()
    parent.close()
    return Repo.clone_from(str(path.parent / f"{path.name}-src"), path, bare=True)


def _one_submodule(path: Path) -> Repo:
    parent, sub = _with_submodule(path)
    sub.close()
    return parent


SUBMODULE_CASES: dict[str, Callable[[Path], Repo]] = {
    "no .gitmodules": _no_gitmodules,
    "one submodule": _one_submodule,
    ".gitmodules deleted, still in HEAD": _gitmodules_deleted,
    "bare repo with a submodule": _bare_with_submodule,
}


@pytest.mark.parametrize("case", SUBMODULE_CASES)
def test_submodules_match_gitpython(tmp_path: Path, case: str) -> None:
    """Skipping the lookup without `.gitmodules` finds what GitPython finds."""
    with SUBMODULE_CASES[case](tmp_path / "repo") as repo:
        fast = [s.path for s in _submodules(repo)]
        clear = [s.path for s in repo.submodules]
    assert fast == clear


# --- checkout paths: `%(worktreepath)` vs `git worktree list` ---------------


def _worktree_list_checkouts(repo: Repo) -> dict[str, str]:
    """Map each checked-out branch to its worktree the clear way.

    Parses `git worktree list --porcelain`, skipping bare and prunable entries,
    and leaves out the scanned work tree itself.
    """
    own = Path(repo.working_dir).resolve()
    checkouts: dict[str, str] = {}
    for record in repo.git.worktree("list", "--porcelain").split("\n\n"):
        lines = record.splitlines()
        if "bare" in lines or any(line.startswith("prunable") for line in lines):
            continue
        path = next(line for line in lines if line.startswith("worktree "))
        branch = next((line for line in lines if line.startswith("branch ")), None)
        resolved = Path(path.removeprefix("worktree ")).resolve()
        if branch is not None and resolved != own:
            name = branch.removeprefix("branch refs/heads/")
            checkouts[name] = resolved.as_posix()
    return checkouts


def _scan_checkouts(repo: Repo) -> dict[str, str]:
    statuses = all_branches_status(repo)
    return {
        name: str(status["worktree"])
        for name, status in statuses.items()
        if "worktree" in status
    }


def _linked_worktrees(tmp_path: Path) -> tuple[Repo, Repo]:
    """Make a repo with linked worktrees: one live and two that git calls prunable.

    One prunable worktree is deleted from disk; the other keeps its directory
    but has lost its `.git` file.
    """
    repo = _init(tmp_path / "repo")
    repo.git.worktree("add", str(tmp_path / "wt"), "-b", "feat")
    repo.git.worktree("add", str(tmp_path / "gone"), "-b", "stale")
    shutil.rmtree(tmp_path / "gone")
    repo.git.worktree("add", str(tmp_path / "unlinked"), "-b", "orphan")
    (tmp_path / "unlinked" / ".git").unlink()
    return repo, Repo(tmp_path / "wt")


def test_checkouts_match_worktree_list(tmp_path: Path) -> None:
    """Each side of a linked worktree names the other, as `worktree list` does."""
    repo, linked = _linked_worktrees(tmp_path)
    with repo, linked:
        for scanned in (repo, linked):
            assert _scan_checkouts(scanned) == _worktree_list_checkouts(scanned)
        assert _scan_checkouts(repo) == {"feat": (tmp_path / "wt").resolve().as_posix()}


def test_bare_repo_checkouts_match_worktree_list(tmp_path: Path) -> None:
    """A bare repo names its linked worktree, and no worktree for its HEAD."""
    _init(tmp_path / "src").close()
    with Repo.clone_from(str(tmp_path / "src"), tmp_path / "bare", bare=True) as repo:
        repo.git.worktree("add", str(tmp_path / "wt"), "-b", "feat")
        assert _scan_checkouts(repo) == _worktree_list_checkouts(repo)
        assert list(_scan_checkouts(repo)) == ["feat"]


def test_submodule_checkout_differs_from_worktree_list(tmp_path: Path) -> None:
    """Intended difference: a submodule's own branch gets no `worktree` field.

    `git worktree list` names the submodule's git dir as its main worktree, so
    the clear version reports the submodule's own branch as checked out
    elsewhere, at `<parent>/.git/modules/sm`. The scan maps that path back to
    the submodule's work tree, which is the repo itself.
    """
    parent, sub = _with_submodule(tmp_path / "repo")
    with parent, sub:
        sub.git.switch("-c", "work")
        assert _scan_checkouts(sub) == {}


# --- git error text: parsing GitPython's stderr wrapper ---------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["ls-remote", "dead"], id="several lines"),
        # a single line ending in a quote, next to GitPython's own closing quote
        pytest.param(["--git-dir", "{missing}", "status"], id="ends in a quote"),
    ],
)
def test_error_message_is_git_stderr(tmp_path: Path, command: list[str]) -> None:
    """The error line pulled out of GitPython's exception is git's own stderr."""
    args = [arg.format(missing=tmp_path / "missing") for arg in command]
    with _init(tmp_path / "repo") as repo:
        repo.create_remote("dead", str(tmp_path / "missing"))
        with pytest.raises(GitCommandError) as raised:
            repo.git.execute(["git", *args])
        # the same command, with git's stderr returned as is instead of raised
        _status, _stdout, stderr = repo.git.execute(
            ["git", *args], with_extended_output=True, with_exceptions=False
        )
    assert isinstance(stderr, str)
    first_error = next(
        line for line in stderr.splitlines() if line.startswith(("fatal:", "error:"))
    )
    assert _git_error_message(raised.value) == first_error
