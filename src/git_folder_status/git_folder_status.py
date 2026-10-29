"""Find all subdirectories with uncommitted or unpushed code.

This code scans through a directory recursively to identify the status of
all Git repositories found within.

Run `git-folder-status -h` for help.
"""

import functools
import sys
from collections import ChainMap
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from git import Git, GitCommandError, InvalidGitRepositoryError, Repo
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
    str, "str | int | bool | list[str] | RepoStats | list[RepoStats] | None"
]

# Stats that live in the shared object/ref store and are therefore identical
# across every worktree of a repo. Everything else (dirty state, untracked
# files, detached HEAD, submodules) is specific to one working tree.
SHARED_REPO_KEYS = frozenset(
    {
        "stash_count",
        "remotes",
        "remotes_unreachable",
        "branches",
        "local_tags",
        "tags_local_only",
        "tags_mismatch",
    }
)


@dataclass(frozen=True)
class ScanOptions:
    """Flags controlling how each repo is analyzed, threaded through the scan.

    `slow` allows expensive operations: listing each remote over the network,
    to compare tags and to check that remote branches still exist. `include_all`
    reports context beyond issues. `include_behind` keeps branches that are
    only behind their upstream.

    `remote_listings` caches each repo's `RemoteListing` by its common git
    dir, so the worktrees of one repo share a single round of network calls.
    """

    slow: bool = False
    include_all: bool = False
    include_behind: bool = False
    remote_listings: dict[Path, "RemoteListing"] = field(
        default_factory=dict, repr=False, compare=False
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


DEFAULT_BRANCH_NAMES = frozenset({"main", "master"})


@dataclass(frozen=True)
class RemoteListing:
    """The refs each remote actually holds, as `git ls-remote` reports them.

    Local `refs/remotes/*` only cache the last fetch: a branch deleted on the
    remote, or a remote that no longer exists, still looks pushed. `refs` maps
    each listed remote to its `{refname: sha}`; `errors` maps each remote that
    could not be listed to git's error message.
    """

    refs: dict[str, dict[str, str]]
    errors: dict[str, str]

    def ref_state(self, remote: str, remote_head: str) -> str:
        """Return `unreachable`, `gone` or `ok` for branch `remote_head` on `remote`."""
        if remote in self.errors:
            return "unreachable"
        if f"refs/heads/{remote_head}" not in self.refs.get(remote, {}):
            return "gone"
        return "ok"

    def tracking_ref_state(self, ref: RemoteReference) -> str:
        """Return `ref_state` for a local remote-tracking ref."""
        return self.ref_state(ref.remote_name, ref.remote_head)


@dataclass(frozen=True)
class BranchRef:
    """A local branch and its upstream, as one `git for-each-ref` reports them.

    `upstream` is the short remote-tracking ref (`origin/feature`), empty when
    no upstream is configured or it is a local branch. `remote` and
    `remote_head` name the branch on that remote. `track` is git's own
    `ahead N, behind M` summary of the branch against its upstream, or `gone`.
    """

    name: str
    head: str
    upstream: str = ""
    remote: str = ""
    remote_head: str = ""
    track: str = ""


# fields separated by NUL (`%00`), which no ref name can contain
_BRANCH_FORMAT = (
    "%(refname)%00"
    "%(objectname)%00"
    "%(upstream)%00"
    "%(upstream:remotename)%00"
    "%(upstream:remoteref)%00"
    "%(upstream:track,nobracket)"
)


def list_branches(repo: Repo) -> list[BranchRef]:
    """Return every local branch with its upstream, in one git call."""
    out = repo.git.for_each_ref(f"--format={_BRANCH_FORMAT}", "refs/heads")
    branches = []
    for line in out.splitlines():
        refname, head, upstream, remote, remote_ref, track = line.split("\0")
        # an upstream outside refs/remotes/ is a local branch, not a remote
        if not upstream.startswith("refs/remotes/"):
            upstream = remote = remote_ref = track = ""
        branches.append(
            BranchRef(
                name=refname.removeprefix("refs/heads/"),
                head=head,
                upstream=upstream.removeprefix("refs/remotes/"),
                remote=remote,
                remote_head=remote_ref.removeprefix("refs/heads/"),
                track=track,
            )
        )
    return branches


def _parse_track(track: str) -> tuple[int, int]:
    """Return (ahead, behind) from git's `ahead N, behind M` summary."""
    counts = {"ahead": 0, "behind": 0}
    for part in track.split(", "):
        if part:
            key, count = part.split(" ")
            counts[key] = int(count)
    return counts["ahead"], counts["behind"]


# namespaces searched for a ref holding a detached HEAD, most informative first:
# a remote ref means the commit is pushed, a local branch only that it is kept
_HEAD_CONTAINER_NAMESPACES = ("refs/remotes/", "refs/tags/", "refs/heads/")


def _ref_containing_head(repo: Repo) -> str | None:
    """Return a ref whose history contains HEAD, or `None` if none does.

    A detached HEAD reachable from no ref holds commits that `git gc` will
    eventually drop once HEAD moves away. Prefers remote refs, then tags, then
    local branches, and within each a main/master branch.
    """
    out = repo.git.for_each_ref(
        "--contains",
        "HEAD",
        "--format=%(refname) %(refname:short)",
        *_HEAD_CONTAINER_NAMESPACES,
    )
    candidates: list[tuple[int, bool, str]] = []
    for line in out.splitlines():
        refname, short = line.split(" ", 1)
        if refname.startswith("refs/remotes/") and refname.endswith("/HEAD"):
            continue  # a symbolic alias of a remote branch listed on its own
        rank = next(
            i
            for i, ns in enumerate(_HEAD_CONTAINER_NAMESPACES)
            if refname.startswith(ns)
        )
        is_default = refname.rsplit("/", 1)[-1] in DEFAULT_BRANCH_NAMES
        candidates.append((rank, not is_default, short))
    return min(candidates)[2] if candidates else None


@dataclass(frozen=True)
class WorkTreeStatus:
    """A work tree's state, as one `git status --porcelain=v2` call reports it.

    `head` is the commit sha, `None` in a repo with no commits yet. `branch`
    is the checked-out branch, `None` on a detached HEAD.
    """

    head: str | None
    branch: str | None
    is_dirty: bool
    untracked_files: list[str]
    stash_count: int


# `git status --porcelain=v2 --show-stash` reports the stash count from 2.35 on;
# older git prints nothing, which would hide stashed work as "no stash"
_STATUS_SHOWS_STASH = (2, 35)


@functools.cache
def _git_version() -> tuple[int, ...]:
    """Return the git version, asked once per process.

    GitPython caches `version_info` per `Repo`, which costs a `git version`
    call for every scanned repo.
    """
    return Git().version_info


def work_tree_status(repo: Repo) -> WorkTreeStatus:
    """Return HEAD, changes, untracked files and stash count in one git call.

    Matches GitPython's `is_dirty()` (staged, unstaged and submodule changes,
    not untracked files) and `untracked_files` (each file inside untracked dirs).
    A submodule with only untracked files is not a change here: like
    `is_dirty()`, and those files are reported under the submodule itself.
    """
    out = repo.git.status(
        "--porcelain=v2",
        "--branch",
        # `--branch` would also count commits against the upstream, unused here
        "--no-ahead-behind",
        "--show-stash",
        "--untracked-files=all",
        "--ignore-submodules=untracked",
        "-z",
    )
    head: str | None = None
    branch: str | None = None
    is_dirty = False
    untracked: list[str] = []
    stash_count = 0
    records = iter(out.split("\0"))
    for record in records:
        kind, _, rest = record.partition(" ")
        if kind == "#":
            key, _, value = rest.partition(" ")
            if key == "branch.oid" and value != "(initial)":
                head = value
            elif key == "branch.head" and value != "(detached)":
                branch = value
            elif key == "stash":
                stash_count = int(value)
        elif kind in {"1", "2", "u"}:
            is_dirty = True
            if kind == "2":
                next(records)  # a rename's original path is a record of its own
        elif kind == "?":
            untracked.append(rest)
    if _git_version() < _STATUS_SHOWS_STASH:
        stash_count = len(repo.git.stash("list").splitlines())
    return WorkTreeStatus(head, branch, is_dirty, untracked, stash_count)


def _bare_status(repo: Repo) -> WorkTreeStatus:
    """Return the HEAD of a bare repo; it has no work tree, changes or stash."""
    try:
        head: str | None = repo.head.commit.hexsha
    except ValueError:
        head = None  # an empty repo
    branch = None if repo.head.is_detached else repo.active_branch.name
    return WorkTreeStatus(
        head, branch, is_dirty=False, untracked_files=[], stash_count=0
    )


def repo_stats(repo: Repo, *, include_remotes: bool = True) -> RepoStats:
    """Return stats for a repo.

    `remotes` costs one git call per remote and is context, not an issue, so
    `include_remotes=False` leaves it out.
    """
    bare = repo.bare
    # `git status` fails without a work tree
    status = _bare_status(repo) if bare else work_tree_status(repo)
    detached = status.branch is None
    return {
        "bare": bare,
        "is_dirty": status.is_dirty,
        "untracked_files": shorten_list(status.untracked_files),
        "is_detached_head": detached,
        "detached_head_contained_in": (
            _ref_containing_head(repo) if detached and status.head else None
        ),
        "active_branch": status.branch,
        "head_commit_hash_short": status.head[:7] if status.head else None,
        "remotes": (
            {r.name: list(r.urls) for r in repo.remotes} if include_remotes else None
        ),
        "stash_count": status.stash_count,
    }


def repo_issues_in_stats(repo: Repo, options: ScanOptions) -> RepoStats:
    """Return issues in a repo."""
    stats_to_include = {
        "is_dirty",
        "untracked_files",
        "stash_count",
        "is_detached_head",
        "detached_head_contained_in",
    }
    stats = repo_stats(repo, include_remotes=options.include_all)
    if not options.include_all:
        stats = {k: v for k, v in stats.items() if k in stats_to_include}
    issues: RepoStats = {k: v for k, v in stats.items() if v}
    return issues


def _push_remote(repo: Repo, branch: str) -> str:
    """Return the remote that a plain `git push` sends `branch` to.

    Follows git's own order: `branch.<name>.pushRemote`, `remote.pushDefault`,
    `branch.<name>.remote`, then `origin`. Reads config only, no git call.
    """
    section = f'branch "{branch}"'
    with repo.config_reader() as config:
        for sec, option in (
            (section, "pushRemote"),
            ("remote", "pushDefault"),
            (section, "remote"),
        ):
            value = config.get_value(sec, option, "")
            if value:
                return str(value)
    return "origin"


def _matching_remote_branches(repo: Repo, branch: str) -> list[RemoteReference]:
    """Find the remote branches that match `branch` by name, preferred first.

    Catches branches that were pushed without `-u`:
    the remote branch exists, but no upstream is configured.
    Matches by name suffix, so `origin/user/feature` matches a local `feature`.
    Preferred is `<push remote>/<branch>` on the remote `git push` would use,
    then any `<remote>/<branch>`, then prefixed names, each in config order.
    """
    suffix = f"/{branch}"
    candidates = [
        ref
        for remote in repo.remotes
        for ref in remote.refs
        if ref.name.endswith(suffix)
    ]
    if not candidates:
        return []
    pushed = f"{_push_remote(repo, branch)}{suffix}"
    exact_names = {f"{remote.name}{suffix}" for remote in repo.remotes}
    return sorted(
        candidates,
        key=lambda ref: (ref.name != pushed, ref.name not in exact_names),
    )


def _ahead_behind(repo: Repo, local: str, remote: str) -> tuple[int, int]:
    """Return (ahead, behind): commits on `local` not on `remote`, and vice versa."""
    out = repo.git.rev_list("--left-right", "--count", f"{local}...{remote}")
    ahead, behind = out.split()
    return int(ahead), int(behind)


def _untracked_branch_status(
    repo: Repo, branch: BranchRef, remotes: RemoteListing | None
) -> RepoStats:
    """Return the status of a branch with no upstream configured."""
    head = branch.head
    # no upstream configured, but the branch may still exist on a remote
    candidates = _matching_remote_branches(repo, branch.name)
    if not candidates:
        return {"upstream": "missing", "head": head}
    # the preferred candidate stands in for the upstream: when it is lost,
    # that is reported, not hidden by falling back to another remote
    matching = candidates[0]
    if remotes is not None and (state := remotes.tracking_ref_state(matching)) != "ok":
        return {"upstream": state, "remote_branch": matching.name, "head": head}
    ahead, behind = _ahead_behind(repo, f"refs/heads/{branch.name}", matching.path)
    return {
        "upstream": "unset",
        "remote_branch": matching.name,
        "ahead": ahead,
        "behind": behind,
        "head": head,
    }


def _still_on(
    repo: Repo, branch: str, lost: str, remotes: RemoteListing | None
) -> list[str]:
    """Return the other remote branches matching `branch` that still exist."""
    return [
        ref.name
        for ref in _matching_remote_branches(repo, branch)
        if ref.name != lost
        and (remotes is None or remotes.tracking_ref_state(ref) == "ok")
    ]


# upstream states whose remote branch is lost: another remote may still hold it
_LOST_STATES = frozenset({"gone", "unreachable"})


def branch_status(
    repo: Repo, branch: BranchRef, remotes: RemoteListing | None = None
) -> RepoStats:
    """Return the upstream relationship of a single branch.

    A branch in a `_LOST_STATES` state carries a `still_on` list when other
    remotes hold a branch of the same name: a hint that its work may be safe.

    The `upstream` field discriminates six mutually exclusive states:

    - `set`: a real upstream is configured and its remote ref exists.
    - `unset`: no upstream configured, but a remote branch matches by name
      (typically pushed without `-u`).
      Its ahead/behind counts are measured against that name-matched
      *candidate*, so they are only a guess.
    - `gone`: an upstream is configured but its remote ref no longer exists.
    - `default`: a branch not itself named main/master tracks a remote
      main/master (typically created with `git switch -c feature origin/main`).
      Its ahead/behind counts would only measure drift from the default
      branch, not unpushed work, so they are not computed.
    - `missing`: local-only, no upstream and no matching remote branch.
    - `unreachable`: the remote holding the upstream (or the name-matched
      candidate) could not be listed, so the local remote-tracking ref may be
      stale. Ahead/behind counts against it are not computed.

    Only a `remotes` listing (a network call, under `--slow`) can tell
    `unreachable` apart, and tell a stale remote-tracking ref from a live one:
    with it, a ref the remote no longer holds counts as `gone`. A name-matched
    candidate is checked the same way, as if it were the upstream.
    """
    status = _branch_status(repo, branch, remotes)
    if status["upstream"] in _LOST_STATES:
        lost = status["remote_branch"]
        assert isinstance(lost, str)  # noqa: S101
        still_on = _still_on(repo, branch.name, lost, remotes)
        if still_on:
            status["still_on"] = still_on
    return status


def _branch_status(
    repo: Repo, branch: BranchRef, remotes: RemoteListing | None
) -> RepoStats:
    """Return a branch's upstream state, without the `still_on` hint."""
    head = branch.head
    if not branch.upstream:
        return _untracked_branch_status(repo, branch, remotes)
    remote_branch = branch.upstream
    if branch.track == "gone":
        return {"upstream": "gone", "remote_branch": remote_branch, "head": head}
    if (
        remotes is not None
        and (state := remotes.ref_state(branch.remote, branch.remote_head)) != "ok"
    ):
        return {"upstream": state, "remote_branch": remote_branch, "head": head}
    if (
        branch.remote_head in DEFAULT_BRANCH_NAMES
        and branch.name not in DEFAULT_BRANCH_NAMES
    ):
        return {"upstream": "default", "remote_branch": remote_branch, "head": head}
    ahead, behind = _parse_track(branch.track)
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


# upstream states with no usable ahead count, so containment in a remote
# default branch is the only sign of whether the branch holds unique work
_CONTAINMENT_STATES = frozenset({"missing", "gone", "default", "unreachable"})


def _contained_in_default(
    repo: Repo, remotes: RemoteListing | None = None
) -> dict[str, str]:
    """Map each local branch to a remote main/master that contains its head.

    Runs one `git for-each-ref --merged` per remote default branch, not one
    query per local branch. The first containing ref wins. With a `remotes`
    listing, a main/master the remote no longer holds (or an unreachable
    remote's) does not count: being contained in it keeps nothing safe.
    """
    contained: dict[str, str] = {}
    for remote in repo.remotes:
        for ref in remote.refs:
            if ref.remote_head not in DEFAULT_BRANCH_NAMES:
                continue
            if remotes is not None and remotes.tracking_ref_state(ref) != "ok":
                continue
            out = repo.git.for_each_ref(
                "--merged", ref.name, "--format=%(refname)", "refs/heads"
            )
            for refname in out.splitlines():
                contained.setdefault(refname.removeprefix("refs/heads/"), ref.name)
    return contained


def all_branches_status(
    repo: Repo, remotes: RemoteListing | None = None
) -> dict[str, RepoStats]:
    """Return the upstream status of all branches in a repo.

    A branch checked out in some *other* worktree carries a `worktree` field
    holding that worktree's path, so a diverged branch can be located. A branch
    checked out in `repo` itself gets no such field: the path would only echo
    the repo being reported on, so it is redundant.

    A branch in a `_CONTAINMENT_STATES` state whose head is reachable from a
    remote main/master carries a `contained_in` field naming that ref: it has
    no commits of its own, so deleting it loses nothing.
    """
    branches = [b for b in list_branches(repo) if not b.name.startswith("gitbutler/")]
    if not branches:
        return {}
    checkouts = _worktree_branches(repo)
    own = Path(repo.working_dir).resolve().as_posix() if repo.working_dir else None
    result: dict[str, RepoStats] = {}
    for branch in branches:
        status = branch_status(repo, branch, remotes)
        worktree = checkouts.get(branch.name)
        if worktree is not None and worktree != own:
            status["worktree"] = worktree
        result[branch.name] = status
    # skip the git calls when no branch can use the answer
    if any(st["upstream"] in _CONTAINMENT_STATES for st in result.values()):
        contained = _contained_in_default(repo, remotes)
        for name, status in result.items():
            if status["upstream"] in _CONTAINMENT_STATES and name in contained:
                status["contained_in"] = contained[name]
    return result


def _branch_has_issue(status: RepoStats, *, include_behind: bool) -> bool:
    """Whether a branch's upstream status is worth reporting."""
    # any non-`set` upstream state (missing/unset/gone/default/unreachable) is
    # itself the issue
    if status["upstream"] != "set":
        return True
    if status.get("ahead"):
        return True
    return bool(include_behind and status.get("behind"))


# upstream states reported as a single key naming the configured remote branch
_REMOTE_BRANCH_ISSUE_KEYS = {
    "gone": "remote_deleted",
    "default": "tracks_default_branch",
    "unreachable": "remote_unreachable",
}


def _upstream_issue(status: RepoStats) -> RepoStats:
    """Return the report keys describing a branch's upstream state."""
    upstream = status["upstream"]
    record: RepoStats = {}
    if upstream == "missing":
        record["no_remote"] = True
    elif upstream in _REMOTE_BRANCH_ISSUE_KEYS:
        record[_REMOTE_BRANCH_ISSUE_KEYS[upstream]] = status["remote_branch"]
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
    return record


def _branch_record(status: RepoStats, *, include_all: bool) -> RepoStats:
    """Shape a branch's upstream status for the report.

    A clean, in-sync branch yields `{}`, so the report's generic "drop falsy"
    pass removes it with no special case: every key present marks a real
    deviation. The `--all` view re-adds `remote_branch`/`head` so even clean
    branches stay non-empty and are listed.
    """
    record = _upstream_issue(status)
    if status.get("contained_in"):
        # qualifies the upstream issue: no commits would be lost
        record["contained_in"] = status["contained_in"]
    if status.get("still_on"):
        record["still_on"] = status["still_on"]
    # remote_branch and head are context, not issue triggers: attach them only
    # under --all, so they never keep a clean branch in the regular report.
    if include_all:
        if status.get("remote_branch"):
            record["remote_branch"] = status["remote_branch"]
        record["head"] = status.get("head")
    return record


def repo_issues_in_branches(
    repo: Repo, options: ScanOptions, remotes: RemoteListing | None = None
) -> RepoStats:
    """Return per-branch upstream issues, keyed under `branches`."""
    branches_st = all_branches_status(repo, remotes)
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


def _remote_refs(repo: Repo, remote_name: str) -> dict[str, str]:
    """Map each branch and tag ref of a remote to its sha, per `git ls-remote`."""
    # ls_remote's typed overload widens the result to str | bytes | tuple | ...;
    # the plain (no with_extended_output) call always yields str.
    # GIT_TERMINAL_PROMPT=0: a dead https remote often asks for a login, which
    # would stall the scan; without a prompt it fails and is reported instead.
    out = repo.git.ls_remote(
        "--heads", "--tags", remote_name, env={"GIT_TERMINAL_PROMPT": "0"}
    )
    if not isinstance(out, str):
        raise TypeError(f"unexpected ls-remote output type: {type(out).__name__}")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, ref = line.split("\t")
        refs[ref] = sha
    return refs


def _git_error_message(e: GitCommandError) -> str:
    """Return the line of a failed git command's stderr that names the error.

    That is the first `fatal:`/`error:` line, so a leading warning or `remote:`
    banner is skipped; failing that, the first line.
    """
    # GitPython wraps stderr as "\n  stderr: '<text>'"
    stderr = (e.stderr or "").strip().removeprefix("stderr: ").strip("'")
    lines = stderr.splitlines()
    if not lines:
        return str(e)
    errors = [ln for ln in lines if ln.lower().startswith(("fatal:", "error:"))]
    return (errors or lines)[0]


def list_remotes(repo: Repo) -> RemoteListing:
    """List every remote once, recording a failure instead of raising it.

    One dead remote must not hide the rest of the repo's report, nor the
    other remotes' tags and branches.
    """
    refs: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    for remote in repo.remotes:
        try:
            refs[remote.name] = _remote_refs(repo, remote.name)
        except GitCommandError as e:
            errors[remote.name] = _git_error_message(e)
    return RemoteListing(refs=refs, errors=errors)


def repo_issues_in_tags(
    repo: Repo, options: ScanOptions, remotes: RemoteListing | None = None
) -> RepoStats:
    """Return issues for all tags in a repo.

    Tags are compared with the remotes only when a `remotes` listing is given.
    """
    issues: RepoStats = {}
    # resolving every tag's commit is slow, and only these two uses need it
    if not options.include_all and remotes is None:
        return issues
    local_tags: dict[str, str] = {tag.path: tag.commit.hexsha for tag in repo.tags}
    if options.include_all:
        shown_tags: RepoStats = {**shorten_dict(local_tags)}
        issues["local_tags"] = shown_tags
    if remotes is not None:
        remote_tags: ChainMap[str, str] = ChainMap(*remotes.refs.values())
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
    issue_keys = {
        "ahead",
        "no_remote",
        "remote_not_tracked",
        "remote_deleted",
        "remote_unreachable",
        "tracks_default_branch",
    }
    return "behind" in record and not (issue_keys & record.keys())


def _filter_submodule_issues(issues: RepoStats) -> RepoStats:
    """Filter issues that aren't relevant for submodules.

    Submodules are pinned to specific commits, so being "behind" the remote
    is expected. We only care about branches that are ahead (unpushed commits).
    A branch `contained_in` a remote default branch has no work at risk either.
    A detached HEAD is the normal submodule state, so it is kept only when no
    ref contains it: then commits made in the submodule are at risk.
    """
    filtered = dict(issues)
    if "detached_head_contained_in" in filtered:
        del filtered["is_detached_head"], filtered["detached_head_contained_in"]
    branches = filtered.get("branches")
    if isinstance(branches, dict):
        kept: RepoStats = {
            branch: record
            for branch, record in branches.items()
            if isinstance(record, dict)
            and not _is_branch_behind_only(record)
            and "contained_in" not in record
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
            remotes = None
            if options.slow:
                remotes = options.remote_listings.get(identity.common_dir)
                if remotes is None:
                    remotes = list_remotes(repo)
                    options.remote_listings[identity.common_dir] = remotes
            repo_st = repo_issues_in_stats(repo, options)
            if remotes is not None and remotes.errors:
                repo_st["remotes_unreachable"] = {**remotes.errors}
            branches_st = repo_issues_in_branches(repo, options, remotes)
            tags_st = repo_issues_in_tags(repo, options, remotes)
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
        # gitpython >=3.1.60 rejects a worktree whose gitdir is gone here,
        # older versions only fail later, when a git command is run.
        if is_orphaned_worktree(folder):
            return {"error": "orphaned worktree"}, None
        return ({"is_git": False} if any(folder.glob("*")) else {}), None
    except GitCommandError as e:
        if is_orphaned_worktree(folder):
            return {"error": "orphaned worktree"}, None
        return {"error": f"git error: {_git_error_message(e)}"}, None
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
    is its parent. A submodule's git dir lives in `<parent>/.git/modules/<path>`
    and records its checkout in `core.worktree`, relative to the git dir. For a
    bare repo the shared git dir is the repo itself.
    """
    if common_dir.name == ".git":
        return common_dir.parent
    try:
        with Repo(common_dir) as repo:
            worktree = repo.config_reader("repository").get_value(
                "core", "worktree", ""
            )
    except (InvalidGitRepositoryError, GitCommandError):
        return common_dir
    return (common_dir / str(worktree)).resolve() if worktree else common_dir


def _enclosing_repo(folder: Path, repo_folders: Iterable[Path]) -> Path | None:
    """Return the deepest scanned repo folder that strictly contains `folder`."""
    resolved = folder.resolve()
    containing = [f for f in repo_folders if f.resolve() in resolved.parents]
    if not containing:
        return None
    return max(containing, key=lambda f: len(f.resolve().parts))


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

    When the main worktree is a submodule checkout of a scanned repo it is not
    scanned as a folder of its own, but its state is already reported inline
    under the parent's `/<submodule path>` key; the linked worktrees are nested
    there rather than emitted as a separate top-level entry.
    """
    groups: dict[Path, list[Path]] = {}
    for folder, identity in identities.items():
        groups.setdefault(identity.common_dir, []).append(folder)

    grouped: dict[str, RepoStats] = {}
    for folder, stats in issues_by_path.items():
        if folder not in identities:
            grouped[_relative_key(folder, basedir)] = stats

    submodule_groups: list[tuple[Path, str, RepoStats]] = []
    for common_dir, folders in groups.items():
        linked = [f for f in folders if identities[f].is_linked_worktree]
        mains = [f for f in folders if not identities[f].is_linked_worktree]
        if not linked:
            for folder in folders:
                grouped[_relative_key(folder, basedir)] = issues_by_path[folder]
            continue
        by_key: RepoStats = {
            _relative_key(folder, basedir): _split_shared_stats(issues_by_path[folder])[
                1
            ]
            for folder in linked
        }
        worktrees: RepoStats = {k: by_key[k] for k in sorted(by_key)}
        if mains:
            main_folder = mains[0]
            entry = dict(issues_by_path[main_folder])
        else:
            main_folder = _main_worktree_dir(common_dir)
            host = _enclosing_repo(main_folder, identities)
            if host is not None:
                key = "/" + main_folder.resolve().relative_to(host.resolve()).as_posix()
                submodule_groups.append((host, key, worktrees))
                continue
            # the main worktree was not scanned: host the shared state here
            shared, _ = _split_shared_stats(issues_by_path[linked[0]])
            entry = dict(shared)
            entry["main_worktree_unscanned"] = True
        entry["worktrees"] = worktrees
        grouped[_relative_key(main_folder, basedir)] = entry

    _attach_submodule_worktrees(grouped, submodule_groups, basedir)
    return grouped


def _attach_submodule_worktrees(
    grouped: dict[str, RepoStats],
    submodule_groups: list[tuple[Path, str, RepoStats]],
    basedir: Path,
) -> None:
    """Nest each `(host repo, submodule key, worktrees)` group in place."""
    for host, key, worktrees in submodule_groups:
        host_entry = grouped.setdefault(_relative_key(host, basedir), {})
        sub_entry = host_entry.get(key)
        if not isinstance(sub_entry, dict):
            sub_entry = {}
            host_entry[key] = sub_entry
        sub_entry["worktrees"] = worktrees


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
        return simplify_report(single)

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
    return simplify_report(issues)


def _simplify_branches(stats: RepoStats) -> RepoStats:
    """Lift branches whose only issue is `no_remote` into a flat list.

    Such a branch carries no information beyond its name, so a whole mapping
    entry per branch is noise. Only an *exactly* `{"no_remote": True}` record is
    lifted: a branch that also names the `worktree` holding it, or that carries
    `--all` context like `head`, stays in `branches` with its details intact.
    The new key takes the place of `branches`, which is dropped if it empties.
    """
    branches = stats.get("branches")
    if not isinstance(branches, dict):
        return stats
    bare = [
        name
        for name, record in branches.items()
        if isinstance(record, dict) and record == {"no_remote": True}
    ]
    if not bare:
        return stats
    kept = {name: record for name, record in branches.items() if name not in bare}
    simplified: RepoStats = {}
    for key, value in stats.items():
        if key != "branches":
            simplified[key] = value
            continue
        simplified["branches_without_remote"] = bare
        if kept:
            simplified["branches"] = kept
    return simplified


def simplify_report(issues: dict[str, RepoStats]) -> dict[str, RepoStats]:
    """Post-process a scan report into a terser shape.

    Applies `_simplify_branches` to every repo entry, including the nested ones:
    submodules (keyed `/<path>`) and linked worktrees (under `worktrees`) hold
    entries of the same shape as a top-level repo.
    """
    return {key: _simplify_entry(stats) for key, stats in issues.items()}


def _simplify_entry(stats: RepoStats) -> RepoStats:
    """Simplify one repo entry and every repo entry nested inside it."""
    simplified = _simplify_branches(stats)
    result: RepoStats = {}
    for key, value in simplified.items():
        if key.startswith("/") and isinstance(value, dict):
            result[key] = _simplify_entry(value)
        elif key == "worktrees" and isinstance(value, dict):
            result[key] = {
                path: _simplify_entry(entry) if isinstance(entry, dict) else entry
                for path, entry in value.items()
            }
        else:
            result[key] = value
    return result


def is_file(p: Path) -> bool:
    """Check if a path is a file."""
    try:
        return p.is_file()
    except OSError:
        # broken link, was reported earlier
        return False
