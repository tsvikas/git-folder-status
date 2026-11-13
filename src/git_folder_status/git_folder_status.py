"""Find all subdirectories with uncommitted or unpushed code.

This code scans through a directory recursively to identify the status of
all Git repositories found within.

Run `git-folder-status -h` for help.
"""

import contextlib
import itertools
import sys
import threading
from collections import ChainMap
from collections.abc import Iterable, Iterator
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from git import (
    Git,
    GitCommandError,
    InvalidGitRepositoryError,
    Repo,
    Submodule,
    SymbolicReference,
)

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
        "unsupported_remote_names",
        "branches",
        "local_tags",
        "tags_local_only",
        "tags_mismatch",
    }
)


# Repos whose remotes are listed at once, whatever `jobs` is. Past OpenSSH's
# default `MaxStartups` of 10 handshakes at once, a server drops new
# connections at random, which would read as unreachable remotes.
MAX_REMOTE_LISTINGS = 8


class RemoteListings:
    """Each repo's `RemoteListing`, listed once per scan by its common git dir.

    The worktrees of one repo share their remotes, so they share one round of
    network calls. They may be analyzed on separate threads at once: a lock
    per common git dir makes the later ones wait for the first listing. At
    most `MAX_REMOTE_LISTINGS` repos are listed at once.
    """

    def __init__(self) -> None:
        """Start with no listings."""
        self._lock = threading.Lock()
        self._locks: dict[Path, threading.Lock] = {}
        self._listings: dict[Path, RemoteListing] = {}
        self._network = threading.BoundedSemaphore(MAX_REMOTE_LISTINGS)

    def get(self, common_dir: Path, repo: Repo) -> "RemoteListing":
        """Return the listing for `common_dir`, listing `repo`'s remotes if new."""
        with self._lock:
            lock = self._locks.setdefault(common_dir, threading.Lock())
        with lock:
            if common_dir not in self._listings:
                with self._network:
                    self._listings[common_dir] = list_remotes(repo)
            return self._listings[common_dir]


@dataclass(frozen=True)
class ScanOptions:
    """Flags controlling how each repo is analyzed, threaded through the scan.

    `slow` allows expensive operations: listing each remote over the network,
    to compare tags and to check that remote branches still exist. `include_all`
    reports context beyond issues. `include_unpulled` keeps branches whose only
    divergence is commits to pull from their upstream.

    `remote_listings` holds each repo's `RemoteListing` (see `RemoteListings`).
    """

    slow: bool = False
    include_all: bool = False
    include_unpulled: bool = False
    remote_listings: RemoteListings = field(
        default_factory=RemoteListings, repr=False, compare=False
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


# branch names that count as a remote's default (integration) branch even
# without its `<remote>/HEAD` pointing there; `develop` covers gitflow repos
DEFAULT_BRANCH_NAMES = frozenset({"main", "master", "develop"})


@dataclass(frozen=True)
class _RemoteBranch:
    """A branch on a configured remote, as its local remote-tracking ref holds it.

    The one place that spells a remote-tracking ref: remote names are assumed
    to contain no `/` (see CLAUDE.md), so the remote is the first path part.
    """

    remote: str
    branch: str

    @property
    def name(self) -> str:
        """The short name, as git shows it: `origin/feature`."""
        return f"{self.remote}/{self.branch}"

    @property
    def path(self) -> str:
        """The full ref: `refs/remotes/origin/feature`."""
        return f"refs/remotes/{self.name}"

    @classmethod
    def from_path(cls, remote: str, path: str) -> "_RemoteBranch | None":
        """Return the branch `path` names on `remote`, or `None` if not its ref."""
        prefix = f"refs/remotes/{remote}/"
        if not path.startswith(prefix):
            return None
        return cls(remote, path.removeprefix(prefix))


def remote_branch_names(repo: Repo) -> dict[str, frozenset[str]]:
    """Map each configured remote to the branch names its local refs hold.

    Read once per repo from `refs/remotes/<remote>/`, in Python with no git
    call. The remote's `HEAD`, an alias of one of its branches, is left out.
    Only configured remotes are listed, not refs a removed remote left.
    """
    names: dict[str, frozenset[str]] = {}
    for remote in repo.remotes:
        found = (_RemoteBranch.from_path(remote.name, ref.path) for ref in remote.refs)
        names[remote.name] = frozenset(
            ref.branch for ref in found if ref is not None and ref.branch != "HEAD"
        )
    return names


def remote_default_branches(
    repo: Repo, branch_names: dict[str, frozenset[str]] | None = None
) -> dict[str, frozenset[str]]:
    """Map each configured remote to the names of its default branches.

    A remote's default branch is what `refs/remotes/<remote>/HEAD` points to.
    That ref is set when cloning or by `git remote set-head`, and goes stale if
    the remote's default changes later, so `main` and `master` also count when
    the remote has them. `branch_names` is `remote_branch_names(repo)`, read
    here when not given. Reads refs only, with no git call.
    """
    if branch_names is None:
        branch_names = remote_branch_names(repo)
    defaults: dict[str, frozenset[str]] = {}
    for remote, names in branch_names.items():
        found = set(DEFAULT_BRANCH_NAMES & names)
        try:
            # `.reference` reads where the symbolic ref points, without
            # resolving the commit (which would start a `git cat-file`)
            head = _RemoteBranch(remote, "HEAD").path
            target = SymbolicReference(repo, head).reference.path
        except (TypeError, ValueError):  # no HEAD ref, or not a symbolic one
            target = ""
        default = _RemoteBranch.from_path(remote, target)
        if default is not None and default.branch in names:
            found.add(default.branch)
        defaults[remote] = frozenset(found)
    return defaults


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


@dataclass(frozen=True)
class BranchRef:
    """A local branch and its upstream, as one `git for-each-ref` reports them.

    `upstream` is the short remote-tracking ref (`origin/feature`), empty when
    no upstream is configured or it is a local branch. `remote` and
    `remote_head` name the branch on that remote. `track` is git's own
    `ahead N, behind M` summary of the branch against its upstream, or `gone`.
    `worktree` is the path of the worktree that has the branch checked out.

    Three configured upstreams git does not resolve to a remote-tracking ref:
    `local_upstream` names a local branch the upstream points at,
    `unresolved_upstream` (`origin/feature`) one on a remote that is not
    configured, and `unfetched_upstream` one on a configured remote whose
    fetch settings never fetch it (a single-branch clone).
    """

    name: str
    head: str
    upstream: str = ""
    remote: str = ""
    remote_head: str = ""
    track: str = ""
    worktree: str = ""
    local_upstream: str = ""
    unresolved_upstream: str = ""
    unfetched_upstream: str = ""


# fields separated by NUL (`%00`), which no ref name can contain
_BRANCH_FORMAT = (
    "%(refname)%00"
    "%(objectname)%00"
    "%(upstream)%00"
    "%(upstream:remotename)%00"
    "%(upstream:remoteref)%00"
    "%(upstream:track,nobracket)%00"
    "%(worktreepath)"
)


def list_branches(repo: Repo) -> list[BranchRef]:
    """Return every local branch with its upstream, in one git call.

    An upstream on a remote that is not configured resolves to nothing in git,
    so for branches without one, their `branch.<name>.remote` and `.merge`
    settings are read from the config (in Python, with no git call).
    """
    out = repo.git.for_each_ref(f"--format={_BRANCH_FORMAT}", "refs/heads")
    branches = []
    for line in out.splitlines():
        fields = line.split("\0")
        refname, head, upstream, remote, remote_ref, track, worktree = fields
        # an upstream outside refs/remotes/ is a local branch, not a remote
        local_upstream = ""
        if not upstream.startswith("refs/remotes/"):
            local_upstream = upstream.removeprefix("refs/heads/")
            upstream = remote = remote_ref = track = ""
        branches.append(
            BranchRef(
                name=refname.removeprefix("refs/heads/"),
                head=head,
                upstream=upstream.removeprefix("refs/remotes/"),
                remote=remote,
                remote_head=remote_ref.removeprefix("refs/heads/"),
                track=track,
                worktree=worktree,
                local_upstream=local_upstream,
            )
        )
    if any(not b.upstream and not b.local_upstream for b in branches):
        branches = _with_unresolved_upstreams(repo, branches)
    return branches


def _with_unresolved_upstreams(
    repo: Repo, branches: list[BranchRef]
) -> list[BranchRef]:
    """Fill in an upstream configured for a branch that git resolves to nothing.

    On a remote that is not configured it is `unresolved_upstream`; on a
    configured remote whose fetch settings do not fetch it, `unfetched_upstream`.
    """
    configured = {remote.name for remote in repo.remotes}
    result = []
    with repo.config_reader() as config:
        for branch in branches:
            section = f'branch "{branch.name}"'
            remote = str(config.get_value(section, "remote", ""))
            merge = str(config.get_value(section, "merge", ""))
            if branch.upstream or branch.local_upstream or not remote or not merge:
                result.append(branch)
                continue
            name = _RemoteBranch(remote, merge.removeprefix("refs/heads/")).name
            if remote in configured:
                result.append(replace(branch, unfetched_upstream=name))
            elif remote != ".":
                result.append(replace(branch, unresolved_upstream=name))
            else:
                result.append(branch)
    return result


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


# The git version, asked once at import, before any thread could ask too.
# GitPython's own import already runs `git version`; its `version_info` is
# cached per `Repo`, which would cost a call for every scanned repo.
_GIT_VERSION: tuple[int, ...] = Git().version_info


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
    if _GIT_VERSION < _STATUS_SHOWS_STASH:
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
    """Return issues in a repo.

    A work tree with uncommitted or untracked files also names its checked-out
    branch (`active_branch`), so the change can be tied to a branch; the name
    is context, never an issue on its own.
    """
    stats_to_include = {
        "is_dirty",
        "untracked_files",
        "stash_count",
        "is_detached_head",
        "detached_head_contained_in",
    }
    stats = repo_stats(repo, include_remotes=options.include_all)
    if not options.include_all:
        if stats["is_dirty"] or stats["untracked_files"]:
            stats_to_include.add("active_branch")
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


def _matching_remote_branches(
    branch_names: dict[str, frozenset[str]], branch: str
) -> list[_RemoteBranch]:
    """Find the remote branches that match `branch` by name, in config order.

    Catches branches that were pushed without `-u`:
    the remote branch exists, but no upstream is configured.
    `branch_names` is `remote_branch_names(repo)`: only configured remotes are
    searched, not refs a removed remote left. Matches the branch part of the
    name, by suffix, so `origin/user/feature` matches a local `feature`.
    """
    return [
        _RemoteBranch(remote, name)
        for remote, names in branch_names.items()
        for name in sorted(names)
        if name == branch or name.endswith(f"/{branch}")
    ]


def _ahead_behind(repo: Repo, ref: str, base: str) -> tuple[int, int]:
    """Return (ahead, behind): commits on `ref` not on `base`, and vice versa."""
    out = repo.git.rev_list("--left-right", "--count", f"{ref}...{base}")
    ahead, behind = out.split()
    return int(ahead), int(behind)


def _remote_candidates(  # noqa: PLR0913
    repo: Repo,
    branch: str,
    upstream: str,
    remotes: RemoteListing | None,
    *,
    branch_names: dict[str, frozenset[str]],
    containing: Iterable[_RemoteBranch] = (),
) -> list[tuple[_RemoteBranch, RepoStats]]:
    """Return the remote branches that may hold `branch`'s work, with entries.

    Those are the remote branches named like `branch`, plus `containing`:
    other remote branches holding its commit (a branch stacked on it, or one
    pushed under another name). Each entry has the remote branch's `name`, and
    its `ahead`/`behind` counts against the local branch when non-zero: like
    every count in the report, they describe the remote branch, so `behind`
    counts local commits it lacks. The
    branch's own `upstream` is left out. With a `remotes` listing, a remote
    branch deleted on its remote is left out too (it holds nothing), and one on
    an unreachable remote is kept with `issue: remote_unreachable` and no
    counts, since it may still exist. Order: `<remote>/<branch>` on the remote
    `git push` would use, then on other remotes, then all others; closest
    (fewest differing commits) first in each.
    """
    named = _matching_remote_branches(branch_names, branch)
    found = [
        ref
        for ref in [*named, *(ref for ref in containing if ref not in named)]
        if ref.name != upstream
    ]
    if not found:
        return []
    exact = {_RemoteBranch(remote, branch).name for remote in branch_names}
    pushed = _RemoteBranch(_push_remote(repo, branch), branch).name
    ranked: list[tuple[int, int, _RemoteBranch, RepoStats]] = []
    for ref in found:
        state = "ok" if remotes is None else remotes.ref_state(ref.remote, ref.branch)
        if state == "gone":
            continue
        entry: RepoStats = {"name": ref.name}
        distance = sys.maxsize
        if state == "unreachable":
            entry["issue"] = "remote_unreachable"
        else:
            ahead, behind = _ahead_behind(repo, ref.path, f"refs/heads/{branch}")
            if ahead:
                entry["ahead"] = ahead
            if behind:
                entry["behind"] = behind
            distance = ahead + behind
        rank = 0 if ref.name == pushed else 1 if ref.name in exact else 2
        ranked.append((rank, distance, ref, entry))
    ranked.sort(key=lambda r: r[:2])
    return [(ref, entry) for _rank, _distance, ref, entry in ranked]


def branch_status(  # noqa: PLR0913
    repo: Repo,
    branch: BranchRef,
    remotes: RemoteListing | None = None,
    defaults: dict[str, frozenset[str]] | None = None,
    *,
    branch_names: dict[str, frozenset[str]] | None = None,
    merged: Iterable[str] = (),
    state: RepoStats | None = None,
    containing: Iterable[_RemoteBranch] = (),
) -> RepoStats:
    """Return the upstream relationship of a single branch.

    `defaults` maps each remote to its default branch names, as
    `remote_default_branches` reads them; without it, main/master count.
    `branch_names` is `remote_branch_names(repo)`, read here when not given.
    `merged` names the remote default branches holding the branch's head (see
    `_merged_into`); those not already reported with counts (the usable
    upstream, or a candidate) become `merged_into`, since having no `behind`
    there says the same. `state` is the branch's `_branch_status`, when the
    caller already has it.

    The `upstream` field discriminates mutually exclusive states; every state
    but `set` is an issue (see `_UPSTREAM_ISSUES` for how each is reported):

    - `set`: the configured upstream exists and can be compared with.
    - `not_set`: no upstream is configured.
    - `local`: the upstream is a local branch, so it says nothing about what is
      pushed.
    - `not_configured`: the upstream is on a remote that is not configured.
    - `not_fetched`: the upstream is on a configured remote whose fetch
      settings never fetch it (a single-branch clone), so there is nothing to
      compare with.
    - `gone`: the upstream's remote exists, but the branch was deleted there.
    - `unreachable`: the upstream's remote could not be listed (`--slow`).
    - `default`: the branch tracks its remote's default branch under another
      name (typically created with `git switch -c feature origin/main`); a
      local `main` tracking a remote whose default is `minimax-m3` counts too.

    `remote_branch` names the configured upstream, except for `not_set`.
    Ahead/behind counts are computed for `set` only, from the local branch's
    view as git reports them (`_branch_record` turns them around): against a
    default branch they would measure drift, not unpushed work. Every other
    state carries
    `candidates`, the same-named remote branches plus `containing`, the remote
    branches holding its commit under another name (see `_remote_candidates`).

    Only a `remotes` listing (a network call, under `--slow`) can tell
    `unreachable` apart, and tell a stale remote-tracking ref from a live one:
    with it, a ref the remote no longer holds counts as `gone`.
    """
    status = dict(state or _branch_status(branch, remotes, defaults))
    upstream = str(status.get("remote_branch", ""))
    reported = {upstream} if status["upstream"] == "set" else set()
    if status["upstream"] != "set":
        if branch_names is None:
            branch_names = remote_branch_names(repo)
        candidates = _remote_candidates(
            repo,
            branch.name,
            upstream,
            remotes,
            branch_names=branch_names,
            containing=containing,
        )
        if candidates:
            status["candidates"] = [entry for _ref, entry in candidates]
            reported.update(ref.name for ref, _entry in candidates)
    merged_into = [ref for ref in merged if ref not in reported]
    if merged_into:
        status["merged_into"] = merged_into
    return status


def _branch_status(
    branch: BranchRef,
    remotes: RemoteListing | None,
    defaults: dict[str, frozenset[str]] | None,
) -> RepoStats:
    """Return a branch's upstream state, without its candidates."""
    if not branch.upstream:
        return {**_unresolved_upstream_status(branch), "head": branch.head}
    state = _remote_upstream_state(branch, remotes, defaults)
    status: RepoStats = {
        "upstream": state,
        "remote_branch": branch.upstream,
        "head": branch.head,
    }
    if state == "set":
        status["ahead"], status["behind"] = _parse_track(branch.track)
    return status


def _unresolved_upstream_status(branch: BranchRef) -> RepoStats:
    """Return the state of an upstream that is no remote-tracking ref, if any."""
    if branch.local_upstream:
        return {"upstream": "local", "remote_branch": branch.local_upstream}
    if branch.unresolved_upstream:
        return {
            "upstream": "not_configured",
            "remote_branch": branch.unresolved_upstream,
        }
    if branch.unfetched_upstream:
        return {"upstream": "not_fetched", "remote_branch": branch.unfetched_upstream}
    return {"upstream": "not_set"}


def _remote_upstream_state(
    branch: BranchRef,
    remotes: RemoteListing | None,
    defaults: dict[str, frozenset[str]] | None,
) -> str:
    """Return the state of an upstream that is a remote-tracking ref."""
    if branch.track == "gone":
        return "gone"
    if remotes is not None:
        state = remotes.ref_state(branch.remote, branch.remote_head)
        if state != "ok":
            return state
    remote_defaults = (
        DEFAULT_BRANCH_NAMES if defaults is None else defaults.get(branch.remote, ())
    )
    if branch.remote_head in remote_defaults and branch.name != branch.remote_head:
        return "default"
    return "set"


def _other_worktree(path: str, *, common_dir: Path, own: Path) -> str | None:
    """Return a branch's `%(worktreepath)` if it is a worktree other than `own`.

    `common_dir` and `own` are the scanned repo's resolved git dir and work
    tree. When the main worktree lives apart from its git dir (a submodule, a
    `--separate-git-dir` repo), git names the git dir: that is mapped to the
    real work tree. A bare repo's HEAD names the repo itself, which is no work
    tree. A worktree git calls prunable does not count either: one whose `.git`
    is gone, with or without its directory.
    """
    if not path:
        return None
    resolved = Path(path).resolve()
    if resolved == common_dir:
        resolved = _main_worktree_dir(common_dir)
        if resolved == common_dir:  # a bare repo
            return None
    if resolved == own or not (resolved / ".git").exists():
        return None
    return resolved.as_posix()


def _merged_into(
    repo: Repo, defaults: dict[str, frozenset[str]]
) -> dict[str, list[_RemoteBranch]]:
    """Map each local branch to the remote default branches that contain its head.

    `defaults` holds each configured remote's default branch names. Runs one
    `git for-each-ref --merged` per remote default branch, not one query per
    local branch; refs are listed in remote order. Every local ref counts here;
    `_still_held` drops those a `remotes` listing shows are gone or unreachable.
    """
    merged: dict[str, list[_RemoteBranch]] = {}
    for remote, names in defaults.items():
        for name in sorted(names):
            ref = _RemoteBranch(remote, name)
            out = repo.git.for_each_ref(
                "--merged", ref.path, "--format=%(refname)", "refs/heads"
            )
            for refname in out.splitlines():
                merged.setdefault(refname.removeprefix("refs/heads/"), []).append(ref)
    return merged


def _still_held(
    refs: Iterable[_RemoteBranch], remotes: RemoteListing | None
) -> list[str]:
    """Return the names of `refs` their remote still holds.

    With a `remotes` listing, a default branch the remote no longer holds (or an
    unreachable remote's) does not count: being contained in it keeps nothing
    safe.
    """
    return [
        ref.name
        for ref in refs
        if remotes is None or remotes.ref_state(ref.remote, ref.branch) == "ok"
    ]


def _containing_remote_branches(
    repo: Repo, commit: str, branch_names: dict[str, frozenset[str]]
) -> list[_RemoteBranch]:
    """Return the branches on configured remotes whose history holds `commit`.

    One `git for-each-ref --contains` over each configured remote's refs (the
    remotes of `branch_names`), so refs a removed remote left behind are not
    searched. A remote's `HEAD`, an alias of one of its branches, is skipped.
    """
    remotes = list(branch_names)
    prefixes = [_RemoteBranch(remote, "").path for remote in remotes]
    out = repo.git.for_each_ref("--contains", commit, "--format=%(refname)", *prefixes)
    found = []
    for refname in out.splitlines():
        for remote in remotes:
            ref = _RemoteBranch.from_path(remote, refname)
            if ref is not None and ref.branch != "HEAD":
                found.append(ref)
    return found


def all_branches_status(
    repo: Repo,
    remotes: RemoteListing | None = None,
    *,
    include_all: bool = False,
    include_unpulled: bool = False,
) -> dict[str, RepoStats]:
    """Return the upstream status of all branches in a repo.

    A branch checked out in some *other* worktree carries a `worktree` field
    holding that worktree's path, so a diverged branch can be located. A branch
    checked out in `repo` itself gets no such field: the path would only echo
    the repo being reported on, so it is redundant.

    A branch whose head is reachable from remote default branches (see
    `remote_default_branches`) carries a `merged_into` list naming them: its
    commits are safe there (see `branch_status`). The lists are computed when
    any branch will be reported (see `_branch_has_issue`, with
    `include_unpulled`), or for `include_all`. A reported branch always triggers
    that itself, so whether it gets a list never depends on the other branches.

    A branch whose upstream has an issue also gets, as candidates, the remote
    branches holding its commit under another name (see
    `_containing_remote_branches`), unless its commit is in a local copy of a
    remote default branch, even one a `remotes` listing shows unreachable: such
    a commit is held by every branch made from main since. Such an unreachable
    default branch is listed as a candidate instead, since it may still hold
    the work. With no default branch known for any remote there is no way to
    tell, so then no other names are searched.
    """
    branches = [b for b in list_branches(repo) if not b.name.startswith("gitbutler/")]
    if not branches:
        return {}
    common_dir = Path(repo.common_dir).resolve()
    own = Path(repo.working_dir).resolve()
    branch_names = remote_branch_names(repo)
    defaults = remote_default_branches(repo, branch_names)
    states = {b.name: _branch_status(b, remotes, defaults) for b in branches}
    held: dict[str, list[_RemoteBranch]] = {}
    # skip the git calls when no branch can use the answer
    if include_all or any(
        _branch_has_issue(st, include_unpulled=include_unpulled)
        for st in states.values()
    ):
        held = _merged_into(repo, defaults)
    search_other_names = any(defaults.values())
    result: dict[str, RepoStats] = {}
    for branch in branches:
        containing: list[_RemoteBranch] = []
        if states[branch.name]["upstream"] != "set":
            if held.get(branch.name):
                # merged: no search; a default branch `--slow` could not reach
                # still may hold it, so it is listed as such a candidate
                containing = [
                    ref
                    for ref in held[branch.name]
                    if remotes is not None
                    and remotes.ref_state(ref.remote, ref.branch) == "unreachable"
                ]
            elif search_other_names:
                containing = _containing_remote_branches(
                    repo, branch.head, branch_names
                )
        status = branch_status(
            repo,
            branch,
            remotes,
            defaults,
            branch_names=branch_names,
            merged=_still_held(held.get(branch.name, ()), remotes),
            state=states[branch.name],
            containing=containing,
        )
        worktree = _other_worktree(branch.worktree, common_dir=common_dir, own=own)
        if worktree is not None:
            status["worktree"] = worktree
        result[branch.name] = status
    return result


def _branch_has_issue(status: RepoStats, *, include_unpulled: bool) -> bool:
    """Whether a branch's upstream status is worth reporting."""
    # any non-`set` upstream state is itself the issue
    if status["upstream"] != "set":
        return True
    if status.get("ahead"):
        return True
    return bool(include_unpulled and status.get("behind"))


# how each upstream state other than `set` is reported, as `upstream.issue`
_UPSTREAM_ISSUES = {
    "not_set": "not_set",
    "local": "is_local_branch",
    "not_configured": "remote_not_configured",
    "not_fetched": "remote_branch_not_fetched",
    "gone": "remote_branch_deleted",
    "unreachable": "remote_unreachable",
    "default": "is_default_branch",
}


def _branch_record(status: RepoStats, *, include_all: bool) -> RepoStats:
    """Shape a branch's upstream status for the report.

    Called only for a branch that is reported (see `_branch_has_issue`), or
    for any branch under `--all`. `upstream` holds the configured upstream's
    `name`, and either its `issue` or its non-zero `ahead`/`behind` counts,
    which describe the upstream (`behind` counts unpushed commits); for
    a reported branch one of those is always there. Under `--all`, a clean
    branch gets just the name, plus the branch's `head`. `merged_into` and
    `remote_candidates` qualify the upstream.
    """
    upstream: RepoStats = {}
    if status.get("remote_branch"):
        upstream["name"] = status["remote_branch"]
    state = status["upstream"]
    if state in _UPSTREAM_ISSUES:
        upstream["issue"] = _UPSTREAM_ISSUES[str(state)]
    else:  # set: counts are measured against the real configured upstream
        # the status counts are the local branch's; the report's are the upstream's
        if status.get("behind"):
            upstream["ahead"] = status["behind"]
        if status.get("ahead"):
            upstream["behind"] = status["ahead"]
    record: RepoStats = {"upstream": upstream}
    if status.get("merged_into"):
        record["merged_into"] = status["merged_into"]
    if status.get("candidates"):
        record["remote_candidates"] = status["candidates"]
    # head is context, not an issue trigger: attach it only under --all
    if include_all:
        record["head"] = status.get("head")
    return record


def repo_issues_in_branches(
    repo: Repo, options: ScanOptions, remotes: RemoteListing | None = None
) -> RepoStats:
    """Return per-branch upstream issues, keyed under `branches`."""
    branches_st = all_branches_status(
        repo,
        remotes,
        include_all=options.include_all,
        include_unpulled=options.include_unpulled,
    )
    branches: RepoStats = {}
    for name, status in branches_st.items():
        if not options.include_all and not _branch_has_issue(
            status, include_unpulled=options.include_unpulled
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


# A remote that asks for input would stall the scan; without a prompt it fails
# and is reported instead. GIT_TERMINAL_PROMPT=0 stops git's own (https login)
# prompts. ssh reads the terminal itself, for a key's passphrase or to accept
# an unknown host key; SSH_ASKPASS_REQUIRE=force (OpenSSH 8.4+) sends those to
# SSH_ASKPASS instead, here a program that always fails. Keys in ssh-agent and
# known host keys still work.
_NO_PROMPTS_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "SSH_ASKPASS": "false",
    "SSH_ASKPASS_REQUIRE": "force",
}


def _remote_refs(repo: Repo, remote_name: str) -> dict[str, str]:
    """Map each branch and tag ref of a remote to its sha, per `git ls-remote`."""
    # ls_remote's typed overload widens the result to str | bytes | tuple | ...;
    # the plain (no with_extended_output) call always yields str.
    out = repo.git.ls_remote("--heads", "--tags", remote_name, env=_NO_PROMPTS_ENV)
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
    # GitPython wraps stderr as "\n  stderr: '<text>'"; drop only that one pair
    # of quotes, since <text> can itself end in a quote
    wrapped = (e.stderr or "").strip().removeprefix("stderr: ")
    stderr = wrapped.removeprefix("'").removesuffix("'")
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

    That is, the upstream is only `ahead`. For submodules such a branch is not
    worth reporting (see below), whereas unpushed commits (the upstream is
    `behind`) and any upstream problem still are.
    """
    upstream = record.get("upstream")
    return (
        isinstance(upstream, dict)
        and "ahead" in upstream
        and not upstream.keys() & {"behind", "issue"}
    )


def _filter_submodule_issues(issues: RepoStats) -> RepoStats:
    """Filter issues that aren't relevant for submodules.

    Submodules are pinned to specific commits, so being "behind" the remote
    is expected. We only care about branches that are ahead (unpushed commits).
    A branch `merged_into` a remote default branch has no work at risk either.
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
            and "merged_into" not in record
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


def _submodules(repo: Repo) -> list[Submodule]:
    """Return a repo's submodules, without looking when there can be none.

    In a work tree GitPython reads `.gitmodules` from disk, but first loads the
    HEAD commit, which starts a `git cat-file` process. With no `.gitmodules`
    file there is nothing to find. A bare repo reads it from HEAD instead.
    """
    # `working_tree_dir` is None in a bare repo
    work_tree = repo.working_tree_dir
    if work_tree and not (Path(work_tree) / ".gitmodules").is_file():
        return []
    return list(repo.submodules)


def _remote_issues(repo: Repo, remotes: RemoteListing | None) -> RepoStats:
    """Return repo-level issues with the remotes themselves.

    `remotes_unreachable` names each remote a `remotes` listing (`--slow`)
    could not reach. Remote names are assumed to contain no `/`: branch
    results for such a remote may be wrong, so `unsupported_remote_names`
    names them.
    """
    issues: RepoStats = {}
    if remotes is not None and remotes.errors:
        issues["remotes_unreachable"] = {**remotes.errors}
    unsupported = [remote.name for remote in repo.remotes if "/" in remote.name]
    if unsupported:
        issues["unsupported_remote_names"] = unsupported
    return issues


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
                remotes = options.remote_listings.get(identity.common_dir, repo)
            repo_st = repo_issues_in_stats(repo, options) | _remote_issues(
                repo, remotes
            )
            branches_st = repo_issues_in_branches(repo, options, remotes)
            tags_st = repo_issues_in_tags(repo, options, remotes)
            submodules_st = {
                f"/{submodule.path}": _filter_submodule_issues(
                    issues_for_one_folder(Path(submodule.abspath), options)[0]
                )
                for submodule in _submodules(repo)
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
    """Return the working-tree path of every usable worktree of `repo`.

    Parses `git worktree list --porcelain`. Bare entries (no working tree) and
    prunable entries (pointing at a missing directory) are skipped. The trailing
    empty string flushes the final record, which has no blank line after it.
    """
    out = repo.git.worktree("list", "--porcelain")
    paths: list[Path] = []
    path: Path | None = None
    skip = False
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path, skip = Path(line.removeprefix("worktree ")), False
        elif line == "bare" or line.startswith("prunable"):
            skip = True
        elif line == "" and path is not None:
            if not skip:
                paths.append(path)
            path, skip = None, False
    return paths


def _worktree_paths_of(folder: Path) -> list[Path]:
    """Return the worktree paths of the repo in `folder`, or none if unreadable."""
    try:
        with Repo(folder.resolve()) as repo:
            return _list_worktree_paths(repo)
    except (InvalidGitRepositoryError, GitCommandError):
        return []


def _discover_external_worktrees(
    issues_by_path: dict[Path, RepoStats],
    identities: dict[Path, RepoIdentity],
    options: ScanOptions,
    pool: Executor,
) -> None:
    """Analyze worktrees that live outside the scanned tree, in place.

    All worktrees of a repo share one worktree list, so for each repo found in
    the scan we enumerate its worktrees and analyze any that were not already
    scanned, wherever they live. `issues_by_path` and `identities` are mutated.
    The lists, and then the worktrees, are read on `pool`, and taken in the
    order of the scan.
    """
    scanned = {path.resolve() for path in identities}
    roots: dict[Path, Path] = {}  # the first folder of each repo
    for folder, identity in identities.items():
        roots.setdefault(identity.common_dir, folder)
    new_paths: list[Path] = []
    for worktree_paths in pool.map(_worktree_paths_of, roots.values()):
        for wt_path in worktree_paths:
            if wt_path.resolve() in scanned or not wt_path.is_dir():
                continue
            scanned.add(wt_path.resolve())
            new_paths.append(wt_path)
    results = pool.map(issues_for_one_folder, new_paths, itertools.repeat(options))
    for wt_path, (stats, wt_identity) in zip(new_paths, results, strict=True):
        if wt_identity is None:
            continue
        issues_by_path[wt_path] = stats
        identities[wt_path] = wt_identity


@dataclass(frozen=True)
class _FolderScan:
    """A scanned folder's report and identity (see `issues_for_one_folder`).

    `children` holds the folders under it, when the walk descends into it.
    """

    summary: RepoStats
    identity: RepoIdentity | None
    children: "list[_Entry] | None"


# a folder in the walk, with its analysis on the pool, or already done when
# known without one (a broken symlink)
_Entry = tuple[Path, Future[_FolderScan]]


def _done(scan: _FolderScan) -> Future[_FolderScan]:
    """Return a future that already holds `scan`."""
    future: Future[_FolderScan] = Future()
    future.set_result(scan)
    return future


@dataclass(frozen=True)
class _Walk:
    """Walks a tree, analyzing each folder on `pool` as soon as it is found.

    A folder that is not a repo is listed by the thread that found that out,
    so the walk never waits for the analyses of unrelated folders. Two rules
    keep the threads apart:

    - A task never waits on another task's future; only the calling thread
      collects (see `_collect`). A task waiting on its children would
      deadlock once the waiting tasks fill the pool.
    - A task opens its own `Repo` and never hands it on: a `Repo` keeps
      `git cat-file` pipes, which have no lock.
    """

    pool: Executor
    options: ScanOptions
    exclude_dirs: list[str]

    def submit_children(self, basedir: Path, recurse: int) -> list[_Entry]:
        """List the folders under `basedir`, in glob order, and submit each."""
        entries: list[_Entry] = []
        for folder in basedir.glob("*"):
            if folder.name[0] == "." or folder.name in self.exclude_dirs:
                continue
            if folder.is_symlink():
                if not folder.resolve().exists():
                    link: RepoStats = {"broken_link": folder.readlink().as_posix()}
                    entries.append((folder, _done(_FolderScan(link, None, None))))
                    continue
                if basedir in folder.resolve().parents:
                    continue
            if not folder.is_dir():
                continue
            entries.append((folder, self.pool.submit(self._scan, folder, recurse)))
        return entries

    def _scan(self, folder: Path, recurse: int) -> _FolderScan:
        summary, identity = issues_for_one_folder(folder, self.options)
        children = None
        if not summary.get("is_git", True) and recurse > 0:
            children = self.submit_children(folder, recurse - 1)
        return _FolderScan(summary, identity, children)


def _collect(
    entries: list[_Entry], identities: dict[Path, RepoIdentity]
) -> dict[Path, RepoStats]:
    """Wait for the walk's results, and take them in walk order.

    The report then does not depend on which analysis finishes first.
    """
    issues: dict[Path, RepoStats] = {}
    for folder, entry in entries:
        scan = entry.result()
        if scan.children is None:
            issues[folder] = scan.summary
            if scan.identity is not None:
                identities[folder] = scan.identity
        else:
            issues.update(_nested_repos(folder, _collect(scan.children, identities)))
    return issues


def _nested_repos(
    folder: Path, subfolder_summary: dict[Path, RepoStats]
) -> dict[Path, RepoStats]:
    """Summarize a non-repo folder from the reports of the folders under it."""
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


# Repos analyzed at once. Python's side of every git call (starting the
# process, parsing its output) runs on about one core in total, so more threads
# stop helping at around this many. `--slow`'s network calls have their own
# cap, `MAX_REMOTE_LISTINGS`.
DEFAULT_JOBS = 8


@contextlib.contextmanager
def _thread_pool(jobs: int) -> Iterator[Executor]:
    """Run a thread pool of `jobs` threads; on an error, abandon its work.

    The queued folders are dropped, and the error is raised without waiting
    for the analyses still running: they finish on their own threads. The scan
    only reads, so nothing is lost, and the CLI then exits without waiting for
    them (see `cli._exit`).
    """
    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        yield pool
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()


def issues_for_all_subfolders(  # noqa: PLR0913
    basedir: Path,
    recurse: int,
    exclude_dirs: list[str] | None = None,
    *,
    slow: bool = False,
    include_all: bool = False,
    include_unpulled: bool = False,
    scan_external_worktrees: bool = False,
    jobs: int = DEFAULT_JOBS,
) -> dict[str, RepoStats]:
    """Return issues for all repos in a folder.

    Up to `jobs` repos are analyzed at once, each on a thread of its own; the
    report does not depend on `jobs`. `jobs` must be at least 1.
    """
    if jobs < 1:
        raise ValueError(f"jobs must be at least 1, not {jobs}")
    basedir = Path(basedir)
    options = ScanOptions(
        slow=slow, include_all=include_all, include_unpulled=include_unpulled
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
    with _thread_pool(jobs) as pool:
        walk = _Walk(pool, options, exclude_dirs or [])
        entries = walk.submit_children(basedir, recurse)
        issues_by_path = _collect(entries, identities)
        if scan_external_worktrees:
            _discover_external_worktrees(issues_by_path, identities, options, pool)
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


# the record of a branch with no upstream, no remote copy, and nothing merged
_NO_UPSTREAM_ONLY: RepoStats = {"upstream": {"issue": "not_set"}}


def _simplify_branches(stats: RepoStats) -> RepoStats:
    """Lift branches with no upstream and nothing else to say into a flat list.

    Such a branch carries no information beyond its name, so a whole mapping
    entry per branch is noise. Only an *exactly* `_NO_UPSTREAM_ONLY` record is
    lifted: a branch that also has remote candidates, is merged, names the
    `worktree` holding it, or carries `--all` context like `head`, stays in
    `branches` with its details intact.
    The new key takes the place of `branches`, which is dropped if it empties.
    """
    branches = stats.get("branches")
    if not isinstance(branches, dict):
        return stats
    bare = [
        name
        for name, record in branches.items()
        if isinstance(record, dict) and record == _NO_UPSTREAM_ONLY
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
