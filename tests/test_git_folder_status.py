"""Tests for git_folder_status module."""

from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import pytest
from git import Actor, GitCommandError, Repo

from git_folder_status.git_folder_status import (
    RepoIdentity,
    RepoStats,
    ScanOptions,
    _filter_submodule_issues,
    _list_worktree_paths,
    _relative_key,
    _split_shared_stats,
    _worktree_branches,
    branch_status,
    is_file,
    is_orphaned_worktree,
    issues_for_all_subfolders,
    issues_for_one_folder,
    repo_issues_in_branches,
    repo_issues_in_stats,
    repo_issues_in_tags,
    repo_stats,
    shorten_dict,
    shorten_list,
)


class TestShortenList:
    """Test shorten_list function."""

    def test_short_list_unchanged(self) -> None:
        """Test that lists shorter than limit are unchanged."""
        items = ["a", "b", "c"]
        result = shorten_list(items, limit=10)
        assert result == items

    def test_long_list_truncated(self) -> None:
        """Test that long lists are truncated from middle."""
        items = [f"item{i}" for i in range(20)]
        result = shorten_list(items, limit=10)
        assert len(result) == 10
        assert result[0] == "item0"
        assert result[4] == "item4"
        assert result[5] == "<< 11 more items >>"
        assert result[-1] == "item19"

    def test_exact_limit_unchanged(self) -> None:
        """Test that lists exactly at limit are unchanged."""
        items = [f"item{i}" for i in range(10)]
        result = shorten_list(items, limit=10)
        assert result == items


class TestShortenDict:
    """Test shorten_dict function."""

    def test_short_dict_unchanged(self) -> None:
        """A dict at or below the limit is returned unchanged."""
        items = {f"k{i}": f"v{i}" for i in range(5)}
        assert shorten_dict(items, limit=10) == items

    def test_long_dict_truncated_from_middle(self) -> None:
        """A long dict keeps the ends and inserts a count marker in the middle."""
        items = {f"k{i}": f"v{i}" for i in range(20)}
        result = shorten_dict(items, limit=10)
        assert len(result) == 10
        keys = list(result)
        assert keys[:5] == ["k0", "k1", "k2", "k3", "k4"]
        assert keys[5] == "<< 11 more items >>"
        assert keys[-1] == "k19"
        assert result["k0"] == "v0"


class TestFilterSubmoduleIssues:
    """Test _filter_submodule_issues function."""

    def test_removes_is_detached_head(self) -> None:
        """Test that is_detached_head is filtered out."""
        issues: RepoStats = {
            "is_dirty": True,
            "is_detached_head": True,
        }
        result = _filter_submodule_issues(issues)
        assert "is_detached_head" not in result
        assert result["is_dirty"] is True

    def test_removes_branches_only_behind(self) -> None:
        """Test that branches only behind remote are filtered out."""
        issues: RepoStats = {
            "branches": {
                "main": {"behind": 5},
            },
        }
        result = _filter_submodule_issues(issues)
        # branches should be removed entirely since no branch is ahead
        assert "branches" not in result

    def test_keeps_branches_ahead(self) -> None:
        """Test that branches with commits ahead are kept."""
        issues: RepoStats = {
            "branches": {
                "main": {"ahead": 2},
            },
        }
        result = _filter_submodule_issues(issues)
        assert "branches" in result
        assert "main" in result["branches"]  # type: ignore[operator]

    def test_keeps_branches_both_ahead_and_behind(self) -> None:
        """Test that branches both ahead and behind are kept."""
        issues: RepoStats = {
            "branches": {
                "main": {"ahead": 2, "behind": 3},
            },
        }
        result = _filter_submodule_issues(issues)
        assert "branches" in result
        assert "main" in result["branches"]  # type: ignore[operator]

    def test_keeps_branches_with_upstream_problem(self) -> None:
        """Test that an upstream problem is kept even with no ahead commits."""
        issues: RepoStats = {
            "branches": {
                "local": {"missing_upstream": True},
                "stale": {"gone_upstream": "origin/stale"},
            },
        }
        result = _filter_submodule_issues(issues)
        assert result["branches"] == issues["branches"]

    def test_filters_mixed_branches(self) -> None:
        """Test filtering when some branches are ahead and some only behind."""
        issues: RepoStats = {
            "branches": {
                "main": {"behind": 4},
                "feature": {"ahead": 1},
            },
        }
        result = _filter_submodule_issues(issues)
        assert "branches" in result
        branches = result["branches"]
        assert isinstance(branches, dict)
        assert "main" not in branches  # Only behind, filtered out
        assert "feature" in branches  # Ahead, kept

    def test_preserves_other_issues(self) -> None:
        """Test that other issues are preserved."""
        issues: RepoStats = {
            "is_dirty": True,
            "untracked_files": ["file.txt"],
            "stash_count": 2,
            "branches": {
                "main": {"behind": 4},
            },
        }
        result = _filter_submodule_issues(issues)
        assert result["is_dirty"] is True
        assert result["untracked_files"] == ["file.txt"]
        assert result["stash_count"] == 2
        assert "branches" not in result


class TestRepoStats:
    """Test repo_stats function."""

    def test_repo_stats_normal(self) -> None:
        """Test repo_stats with normal repo."""
        mock_repo = Mock(spec=Repo)
        mock_repo.bare = False
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = ["file1.txt", "file2.txt"]

        mock_head = Mock()
        mock_head.is_detached = False
        mock_commit = Mock()
        mock_commit.hexsha = "1234567890abcdef"
        mock_head.commit = mock_commit
        mock_repo.head = mock_head

        mock_branch = Mock()
        mock_branch.name = "main"
        mock_repo.active_branch = mock_branch

        mock_branches = [Mock(name="main", commit=Mock(hexsha="abc123"))]
        mock_repo.branches = mock_branches

        mock_remotes = [Mock(name="origin", urls=["git@github.com:user/repo.git"])]
        mock_repo.remotes = mock_remotes

        mock_repo.git.stash.return_value = "stash@{0}: WIP\nstash@{1}: changes"

        result = repo_stats(mock_repo)

        assert result["is_dirty"] is True
        assert result["untracked_files"] == ["file1.txt", "file2.txt"]
        assert result["is_detached_head"] is False
        assert result["active_branch"] == "main"
        assert result["head_commit_hash_short"] == "1234567"
        assert result["stash_count"] == 2

    def test_repo_stats_empty_repo(self) -> None:
        """Test repo_stats with empty repo (no commits)."""
        mock_repo = Mock(spec=Repo)
        mock_repo.bare = False
        mock_repo.is_dirty.return_value = False
        mock_repo.untracked_files = []

        mock_head = Mock()
        mock_head.is_detached = False
        # Simulate accessing .commit property that raises ValueError
        type(mock_head).commit = PropertyMock(
            side_effect=ValueError("Reference at 'HEAD' does not exist")
        )
        mock_repo.head = mock_head

        mock_branch = Mock()
        mock_branch.name = "main"
        mock_repo.active_branch = mock_branch

        mock_repo.branches = []
        mock_repo.remotes = []
        mock_repo.git.stash.return_value = ""

        result = repo_stats(mock_repo)

        assert result["head_commit_hash_short"] is None

    def test_repo_stats_bare_repo(self) -> None:
        """Test that work-tree stats are skipped in a bare repo."""
        mock_repo = Mock(spec=Repo)
        mock_repo.bare = True
        mock_repo.is_dirty.return_value = False

        mock_head = Mock()
        mock_head.is_detached = False
        mock_head.commit = Mock(hexsha="1234567890abcdef")
        mock_repo.head = mock_head

        mock_repo.active_branch = Mock()
        mock_repo.active_branch.name = "main"
        mock_repo.branches = []
        mock_repo.remotes = []

        result = repo_stats(mock_repo)

        assert result["bare"] is True
        assert result["untracked_files"] == []
        assert result["stash_count"] == 0
        # the underlying git commands fail without a work tree
        mock_repo.git.stash.assert_not_called()


class TestRepoIssuesInStats:
    """Test repo_issues_in_stats function."""

    def test_only_issues_returned(self) -> None:
        """Test that only non-empty issues are returned."""
        mock_repo = Mock(spec=Repo)

        with patch("git_folder_status.git_folder_status.repo_stats") as mock_repo_stats:
            mock_repo_stats.return_value = {
                "is_dirty": True,
                "untracked_files": ["file.txt"],
                "stash_count": 0,
                "is_detached_head": False,
                "active_branch": "main",
            }

            result = repo_issues_in_stats(mock_repo, ScanOptions())

            # Only non-zero/non-empty values should be returned
            expected = {
                "is_dirty": True,
                "untracked_files": ["file.txt"],
            }
            assert result == expected

    def test_include_all_flag(self) -> None:
        """Test include_all flag includes all stats."""
        mock_repo = Mock(spec=Repo)

        with patch("git_folder_status.git_folder_status.repo_stats") as mock_repo_stats:
            mock_repo_stats.return_value = {
                "is_dirty": False,
                "untracked_files": [],
                "stash_count": 0,
                "is_detached_head": False,
                "active_branch": "main",
                "branches": {"main": "abc123"},
            }

            result = repo_issues_in_stats(mock_repo, ScanOptions(include_all=True))

            # With include_all=True, should include more fields
            assert "branches" in result


class TestBranchStatus:
    """Test branch_status function."""

    def test_no_tracking_branch(self) -> None:
        """Test branch with no tracking branch."""
        mock_repo = Mock(spec=Repo)
        mock_repo.remotes = []
        mock_branch = Mock()
        mock_branch.commit.hexsha = "abc123"
        mock_branch.tracking_branch.return_value = None

        result = branch_status(mock_repo, mock_branch)
        assert result == {"upstream": "missing", "head": "abc123"}

    def test_local_tracking_branch(self) -> None:
        """Test branch tracking a local branch."""
        mock_repo = Mock(spec=Repo)
        mock_repo.remotes = []
        mock_branch = Mock()
        mock_branch.commit.hexsha = "abc123"
        mock_tracking = Mock()
        mock_tracking.name = ".local_branch"
        mock_branch.tracking_branch.return_value = mock_tracking

        result = branch_status(mock_repo, mock_branch)
        assert result == {"upstream": "missing", "head": "abc123"}

    @staticmethod
    def _repo_with_remote_refs(ref_names: list[str]) -> Mock:
        mock_repo = Mock(spec=Repo)
        mock_remote = Mock()
        mock_remote.name = "origin"
        mock_remote.refs = []
        for ref_name in ref_names:
            mock_ref = Mock()
            mock_ref.name = ref_name
            mock_remote.refs.append(mock_ref)
        mock_repo.remotes = [mock_remote]
        mock_repo.iter_commits.return_value = iter([])
        return mock_repo

    def test_no_tracking_branch_with_matching_remote(self) -> None:
        """Test branch pushed without `-u`: remote exists, no upstream set."""
        mock_repo = self._repo_with_remote_refs(["origin/user/feature"])
        mock_branch = Mock()
        mock_branch.name = "feature"
        mock_branch.commit.hexsha = "abc123"
        mock_branch.tracking_branch.return_value = None

        result = branch_status(mock_repo, mock_branch)
        assert result == {
            "upstream": "unset",
            "remote_branch": "origin/user/feature",
            "ahead": 0,
            "behind": 0,
            "head": "abc123",
        }

    def test_matching_remote_prefers_exact_name(self) -> None:
        """Test that `origin/feature` is preferred over prefixed variants."""
        mock_repo = self._repo_with_remote_refs(
            ["origin/user/feature", "origin/feature"]
        )
        mock_branch = Mock()
        mock_branch.name = "feature"
        mock_branch.commit.hexsha = "abc123"
        mock_branch.tracking_branch.return_value = None

        result = branch_status(mock_repo, mock_branch)
        assert result["remote_branch"] == "origin/feature"

    def test_matching_remote_requires_full_name_component(self) -> None:
        """Test that `origin/other-feature` does not match a local `feature`."""
        mock_repo = self._repo_with_remote_refs(["origin/other-feature"])
        mock_branch = Mock()
        mock_branch.name = "feature"
        mock_branch.commit.hexsha = "abc123"
        mock_branch.tracking_branch.return_value = None

        result = branch_status(mock_repo, mock_branch)
        assert result == {"upstream": "missing", "head": "abc123"}

    def test_missing_remote_branch(self) -> None:
        """Test branch tracking a missing remote branch."""
        mock_repo = Mock(spec=Repo)
        mock_branch = Mock()
        mock_branch.name = "feature"
        mock_branch.commit.hexsha = "abc123"
        mock_tracking = Mock()
        mock_tracking.name = "origin/feature"
        mock_branch.tracking_branch.return_value = mock_tracking

        mock_repo.refs = {}  # Remote branch doesn't exist

        result = branch_status(mock_repo, mock_branch)
        assert result == {
            "upstream": "gone",
            "remote_branch": "origin/feature",
            "head": "abc123",
        }


class TestRepoIssuesInBranches:
    """Test repo_issues_in_branches function."""

    def test_branches_local_only(self) -> None:
        """Test detection of branches that exist only locally."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                },
                "feature": {"upstream": "missing"},
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            assert result["branches"] == {"feature": {"missing_upstream": True}}

    def test_branches_upstream_unset(self) -> None:
        """Test that pushed-but-untracked branches get their own category."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "feature": {
                    "upstream": "unset",
                    "remote_branch": "origin/user/feature",
                    "behind": 0,
                    "ahead": 2,
                },
                "local-only": {"upstream": "missing"},
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            assert result["branches"] == {
                "feature": {
                    "unset_upstream": {
                        "candidate": "origin/user/feature",
                        "ahead": 2,
                    },
                },
                "local-only": {"missing_upstream": True},
            }

    def test_include_all_branches(self) -> None:
        """Test include_all flag includes synced branches."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "head": "abc123",
                },
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions(include_all=True))

            assert result["branches"] == {
                "main": {"remote_branch": "origin/main", "head": "abc123"},
            }

    def test_include_all_local_only_branch(self) -> None:
        """A local-only branch under --all reports its head but no remote."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "wip": {"upstream": "missing", "head": "abc123"},
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions(include_all=True))

            assert result["branches"] == {
                "wip": {"missing_upstream": True, "head": "abc123"},
            }

    def test_branches_upstream_gone(self) -> None:
        """A configured upstream whose ref was deleted is reported as gone."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "stale": {"upstream": "gone", "remote_branch": "origin/stale"},
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            assert result["branches"] == {
                "stale": {"gone_upstream": "origin/stale"},
            }

    def test_unset_upstream_keeps_behind_in_candidate(self) -> None:
        """An unset branch behind its candidate keeps that count scoped inside."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "feature": {
                    "upstream": "unset",
                    "remote_branch": "origin/feature",
                    "ahead": 0,
                    "behind": 3,
                },
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            assert result["branches"] == {
                "feature": {
                    "unset_upstream": {"candidate": "origin/feature", "behind": 3},
                },
            }

    def test_include_behind_false_filters_behind_only(self) -> None:
        """Test that include_behind=False filters branches only behind."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 5,
                },
                "feature": {
                    "upstream": "set",
                    "remote_branch": "origin/feature",
                    "ahead": 2,
                    "behind": 0,
                },
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            # main is only behind (needs pull) - should be filtered out
            # feature is ahead - should be included
            branches = result["branches"]
            assert isinstance(branches, dict)
            assert "main" not in branches
            assert branches["feature"] == {"ahead": 2}

    def test_include_behind_true_includes_behind_only(self) -> None:
        """Test that include_behind=True includes branches only behind."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 5,
                },
                "feature": {
                    "upstream": "set",
                    "remote_branch": "origin/feature",
                    "ahead": 2,
                    "behind": 0,
                },
            }

            result = repo_issues_in_branches(
                mock_repo, ScanOptions(include_behind=True)
            )

            # Both branches should be included with include_behind=True
            branches = result["branches"]
            assert isinstance(branches, dict)
            assert branches["main"] == {"behind": 5}
            assert branches["feature"] == {"ahead": 2}

    def test_worktree_attached_to_reported_branch(self) -> None:
        """A branch checked out in a worktree names it, but only if it has an issue."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "feature": {
                    "upstream": "set",
                    "remote_branch": "origin/feature",
                    "ahead": 2,
                    "behind": 0,
                    "worktree": "/repo/feature-wt",
                },
                # clean and in-sync: not reported, so its worktree is not named
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "worktree": "/repo/main",
                },
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions())

            branches = result["branches"]
            assert isinstance(branches, dict)
            assert branches == {"feature": {"ahead": 2, "worktree": "/repo/feature-wt"}}

    def test_worktree_attached_under_include_all(self) -> None:
        """Under --all, even a clean checked-out branch names its worktree."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "upstream": "set",
                    "remote_branch": "origin/main",
                    "ahead": 0,
                    "behind": 0,
                    "head": "abc123",
                    "worktree": "/repo/main",
                },
            }

            result = repo_issues_in_branches(mock_repo, ScanOptions(include_all=True))

            branches = result["branches"]
            assert isinstance(branches, dict)
            assert branches["main"] == {
                "remote_branch": "origin/main",
                "head": "abc123",
                "worktree": "/repo/main",
            }


class TestRepoIssuesInTags:
    """Test repo_issues_in_tags function."""

    def test_include_all_local_tags(self) -> None:
        """Test include_all flag includes local tags."""
        mock_repo = Mock(spec=Repo)
        mock_tag = Mock()
        mock_tag.path = "refs/tags/v1.0.0"
        mock_tag.commit.hexsha = "abc123"
        mock_repo.tags = [mock_tag]

        result = repo_issues_in_tags(mock_repo, ScanOptions(include_all=True))

        assert result["local_tags"] == {"refs/tags/v1.0.0": "abc123"}

    def test_slow_mode_tag_comparison(self) -> None:
        """Test slow mode compares with remote tags."""
        mock_repo = Mock(spec=Repo)
        mock_tag = Mock()
        mock_tag.path = "refs/tags/v1.0.0"
        mock_tag.commit.hexsha = "abc123"
        mock_repo.tags = [mock_tag]

        mock_remote = Mock()
        mock_remote.name = "origin"
        mock_repo.remotes = [mock_remote]

        # Mock git ls-remote output
        mock_repo.git.ls_remote.return_value = (
            "def456\trefs/tags/v1.0.0^{}\ndef456\trefs/tags/v1.0.0"
        )

        result = repo_issues_in_tags(mock_repo, ScanOptions(slow=True))

        # Should detect mismatch between local and remote tags
        assert "tags_mismatch" in result


class TestIssuesForOneFolder:
    """Test issues_for_one_folder function."""

    def test_invalid_git_repository(self, tmp_path: Path) -> None:
        """Test handling of non-git directory."""
        # Create some files to make it non-empty
        (tmp_path / "file.txt").write_text("content")

        result, identity = issues_for_one_folder(tmp_path, ScanOptions())
        assert result == {"is_git": False}
        assert identity is None

    def test_empty_non_git_directory(self, tmp_path: Path) -> None:
        """Test handling of empty non-git directory."""
        result, identity = issues_for_one_folder(tmp_path, ScanOptions())
        assert result == {}
        assert identity is None

    def test_bare_repository(self, tmp_path: Path) -> None:
        """Test that a bare repo is analyzed without errors."""
        with Repo.init(tmp_path, bare=True):
            pass

        result, identity = issues_for_one_folder(tmp_path, ScanOptions())
        assert result == {}
        # a bare repo is its own common dir, so it is not a linked worktree
        assert identity is not None
        assert identity.is_linked_worktree is False

        result, _ = issues_for_one_folder(tmp_path, ScanOptions(include_all=True))
        assert "error" not in result
        assert result["bare"] is True

    def test_orphaned_worktree(self, tmp_path: Path) -> None:
        """Test handling of orphaned git worktree."""
        # Create a .git file pointing to a nonexistent worktree directory
        (tmp_path / ".git").write_text("gitdir: /nonexistent/worktree/path\n")
        (tmp_path / "some_file.py").write_text("content")

        result, identity = issues_for_one_folder(tmp_path, ScanOptions())
        assert result == {"error": "orphaned worktree"}
        assert identity is None

    def test_repository_error_handling(self) -> None:
        """Test error handling for repository analysis."""
        folder = Path("/nonexistent")

        with pytest.raises(RuntimeError, match="Error while analyzing repo"):
            issues_for_one_folder(folder, ScanOptions())

    def test_git_command_error_non_orphaned(self, tmp_path: Path) -> None:
        """Test GitCommandError on a folder that is not an orphaned worktree."""
        # Create a directory that looks like a git repo but raises GitCommandError
        (tmp_path / ".git").mkdir()
        with patch("git_folder_status.git_folder_status.Repo") as mock_repo_class:
            mock_repo_class.side_effect = GitCommandError(
                "git status", 128, b"fatal: out of disk space"
            )
            result, identity = issues_for_one_folder(tmp_path, ScanOptions())
        assert "error" in result
        assert "out of disk space" in str(result["error"])
        assert identity is None


class TestIsOrphanedWorktree:
    """Test is_orphaned_worktree function."""

    def test_not_a_worktree(self, tmp_path: Path) -> None:
        """Test regular directory is not an orphaned worktree."""
        assert is_orphaned_worktree(tmp_path) is False

    def test_regular_git_repo(self, tmp_path: Path) -> None:
        """Test regular git repo (.git is a directory) is not orphaned."""
        (tmp_path / ".git").mkdir()
        assert is_orphaned_worktree(tmp_path) is False

    def test_valid_worktree(self, tmp_path: Path) -> None:
        """Test valid worktree is not orphaned."""
        gitdir = tmp_path / "gitdir_target"
        gitdir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        assert is_orphaned_worktree(worktree) is False

    def test_orphaned_worktree(self, tmp_path: Path) -> None:
        """Test worktree pointing to nonexistent gitdir is orphaned."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /nonexistent/path\n")
        assert is_orphaned_worktree(worktree) is True

    def test_relative_gitdir(self, tmp_path: Path) -> None:
        """Test worktree with relative gitdir path."""
        gitdir = tmp_path / "repo" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n")
        assert is_orphaned_worktree(worktree) is False

    def test_git_file_without_gitdir_prefix(self, tmp_path: Path) -> None:
        """Test .git file that doesn't start with 'gitdir:' is not orphaned."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("some other content\n")
        assert is_orphaned_worktree(worktree) is False


class TestIsFile:
    """Test is_file function."""

    def test_normal_file(self, tmp_path: Path) -> None:
        """Test normal file detection."""
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("content")
        assert is_file(test_file) is True

    def test_directory(self, tmp_path: Path) -> None:
        """Test directory is not a file."""
        assert is_file(tmp_path) is False

    def test_broken_symlink(self, tmp_path: Path) -> None:
        """Test broken symlink handling."""
        link_path = tmp_path / "broken_link"
        link_path.symlink_to("nonexistent_target")

        # Should return False for broken symlinks without raising
        assert is_file(link_path) is False


class TestIssuesForAllSubfolders:
    """Test issues_for_all_subfolders function."""

    def test_exclude_dirs(self, tmp_path: Path) -> None:
        """Test directory exclusion."""
        # Create directories
        (tmp_path / "included").mkdir()
        (tmp_path / "excluded").mkdir()
        (tmp_path / ".hidden").mkdir()

        # Add files to make them detectable
        (tmp_path / "included" / "file.txt").write_text("content")
        (tmp_path / "excluded" / "file.txt").write_text("content")

        result = issues_for_all_subfolders(
            tmp_path,
            recurse=1,
            exclude_dirs=["excluded"],
            slow=False,
            include_all=False,
        )

        assert "included" in result
        assert "excluded" not in result
        assert ".hidden" not in result

    def test_broken_symlink_handling(self, tmp_path: Path) -> None:
        """Test handling of broken symlinks."""
        # Create broken symlink
        broken_link = tmp_path / "broken_link"
        broken_link.symlink_to("nonexistent_target")

        result = issues_for_all_subfolders(
            tmp_path, recurse=1, slow=False, include_all=False
        )

        assert "broken_link" in result
        assert result["broken_link"]["broken_link"] == "nonexistent_target"

    def test_recursive_symlink_avoidance(self, tmp_path: Path) -> None:
        """Test avoidance of recursive symlinks."""
        # Create subdirectory and symlink back to parent
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        recursive_link = subdir / "parent_link"
        recursive_link.symlink_to(tmp_path)

        result = issues_for_all_subfolders(
            tmp_path, recurse=2, slow=False, include_all=False
        )

        # Should not include the recursive symlink
        assert "subdir" in result
        # The recursive link should be skipped, not cause infinite recursion

    @patch("sys.version_info", (3, 11, 0))
    def test_python_version_fallback(self, tmp_path: Path) -> None:
        """Test fallback for Python < 3.12."""
        # Create a git repo in temp dir
        with patch("git_folder_status.git_folder_status.Repo") as mock_repo_class:
            mock_repo = Mock()
            mock_repo.working_tree_dir = str(tmp_path)
            mock_repo_class.return_value.__enter__.return_value = mock_repo

            with patch(
                "git_folder_status.git_folder_status.issues_for_one_folder"
            ) as mock_issues:
                mock_issues.return_value = ({"is_dirty": True}, None)

                result = issues_for_all_subfolders(
                    tmp_path, recurse=1, slow=False, include_all=False
                )

                assert "<this repos>" in result

    def test_non_git_directory_with_files_and_symlinks(self, tmp_path: Path) -> None:
        """Test handling of non-git directory with files and symlinks."""
        # Create subdirectory with git repos and non-git content
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        # Create a git repo inside
        git_subdir = subdir / "git_repo"
        git_subdir.mkdir()
        (git_subdir / ".git").mkdir()

        # Create regular files and symlinks in the parent
        (subdir / "regular_file.txt").write_text("content")
        (subdir / "symlink").symlink_to("regular_file.txt")

        with patch(
            "git_folder_status.git_folder_status.issues_for_one_folder"
        ) as mock_issues:

            def side_effect(
                folder: Path, _options: ScanOptions
            ) -> tuple[RepoStats, None]:
                if folder.name == "git_repo":
                    return {"is_dirty": True}, None
                return {"is_git": False}, None

            mock_issues.side_effect = side_effect

            result = issues_for_all_subfolders(
                tmp_path, recurse=2, slow=False, include_all=False
            )

            # Should include both the git repo and note the non-git parent
            assert any("git_repo" in str(k) for k in result)
            assert "subdir" in result
            assert result["subdir"]["is_git"] is False
            assert "untracked_files" in result["subdir"]

    def test_recursive_subfolders_no_loose_files_in_parent(
        self, tmp_path: Path
    ) -> None:
        """Test recursive scan where parent has git repos but no loose files."""
        # basedir/parent/git_repo (a git repo)
        # parent has only subdirectories, no regular files
        parent = tmp_path / "parent"
        parent.mkdir()
        git_repo = parent / "git_repo"
        git_repo.mkdir()

        with patch(
            "git_folder_status.git_folder_status.issues_for_one_folder"
        ) as mock_issues:

            def side_effect(
                folder: Path, _options: ScanOptions
            ) -> tuple[RepoStats, None]:
                if folder.name == "parent":
                    return {"is_git": False}, None
                if folder.name == "git_repo":
                    return {"is_dirty": True}, None
                return {}, None

            mock_issues.side_effect = side_effect

            result = issues_for_all_subfolders(
                tmp_path, recurse=2, slow=False, include_all=False
            )

            # The git_repo should appear in results
            assert any("git_repo" in str(k) for k in result)
            # Parent should NOT have an entry since it has no loose files
            assert "parent" not in result

    def test_basedir_with_loose_files(self, tmp_path: Path) -> None:
        """Test basedir with loose files reports them under '.' key."""
        # Create a file directly in basedir (not inside a git repo)
        (tmp_path / "stray_file.txt").write_text("content")
        # Create a subdirectory too
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        result = issues_for_all_subfolders(
            tmp_path, recurse=1, slow=False, include_all=False
        )

        assert "." in result
        untracked = result["."]["untracked_files"]
        assert isinstance(untracked, list)
        assert "stray_file.txt" in untracked

    def test_directory_with_only_empty_subdirs(self, tmp_path: Path) -> None:
        """Test directory with only empty subdirectories."""
        # Create empty subdirectory
        empty_subdir = tmp_path / "empty_subdir"
        empty_subdir.mkdir()

        with patch(
            "git_folder_status.git_folder_status.issues_for_one_folder"
        ) as mock_issues:
            mock_issues.return_value = ({"is_git": False}, None)

            result = issues_for_all_subfolders(
                tmp_path, recurse=1, slow=False, include_all=False
            )

            # Should mark the empty directory as not git
            assert "empty_subdir" in result
            assert result["empty_subdir"]["is_git"] is False


class TestRepoIdentity:
    """Test RepoIdentity.is_linked_worktree."""

    def test_main_worktree_not_linked(self) -> None:
        """A repo whose git dir equals its common dir is the main worktree."""
        git_dir = Path("/repo/.git")
        identity = RepoIdentity(common_dir=git_dir, git_dir=git_dir)
        assert identity.is_linked_worktree is False

    def test_linked_worktree(self) -> None:
        """A worktree with its own git dir under the common dir is linked."""
        identity = RepoIdentity(
            common_dir=Path("/repo/.git"),
            git_dir=Path("/repo/.git/worktrees/wt"),
        )
        assert identity.is_linked_worktree is True


class TestSplitSharedStats:
    """Test _split_shared_stats."""

    def test_splits_shared_from_local(self) -> None:
        """Repo-level keys go to shared, working-tree keys stay local."""
        stats: RepoStats = {
            "stash_count": 1,
            "branches": {"x": {"missing_upstream": True}},
            "is_dirty": True,
            "untracked_files": ["a.txt"],
        }
        shared, local = _split_shared_stats(stats)
        assert shared == {
            "stash_count": 1,
            "branches": {"x": {"missing_upstream": True}},
        }
        assert local == {"is_dirty": True, "untracked_files": ["a.txt"]}


class TestRelativeKey:
    """Test _relative_key."""

    def test_under_basedir(self, tmp_path: Path) -> None:
        """A path under the base dir becomes a plain relative key."""
        assert _relative_key(tmp_path / "a" / "b", tmp_path) == "a/b"

    def test_outside_basedir_mentions_target(self, tmp_path: Path) -> None:
        """A path outside the base dir still produces a usable key."""
        base = tmp_path / "scan"
        base.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        key = _relative_key(outside, base)
        assert "elsewhere" in key


class TestListWorktreePaths:
    """Test _list_worktree_paths porcelain parsing."""

    def test_skips_bare_and_prunable(self) -> None:
        """Bare entries (no work tree) and prunable entries are skipped."""
        mock_repo = Mock(spec=Repo)
        mock_repo.git.worktree.return_value = (
            "worktree /repo/main\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /repo/.bare\nbare\n\n"
            "worktree /repo/wt\nHEAD def\nbranch refs/heads/wt\n\n"
            "worktree /repo/gone\nHEAD ghi\n"
            "prunable gitdir file points to non-existent location\n"
        )
        assert _list_worktree_paths(mock_repo) == [
            Path("/repo/main"),
            Path("/repo/wt"),
        ]


class TestWorktreeBranches:
    """Test _worktree_branches porcelain parsing."""

    def test_maps_branches_skipping_unusable_entries(self) -> None:
        """Each branch maps to its worktree; bare/detached/prunable are skipped."""
        mock_repo = Mock(spec=Repo)
        mock_repo.git.worktree.return_value = (
            "worktree /repo/main\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /repo/.bare\nbare\n\n"
            "worktree /repo/feat\nHEAD def\nbranch refs/heads/refactor/dotbot\n\n"
            "worktree /repo/detached\nHEAD ghi\ndetached\n\n"
            "worktree /repo/gone\nHEAD jkl\n"
            "prunable gitdir file points to non-existent location\n"
            "branch refs/heads/stale\n"
        )
        # values are `path.resolve().as_posix()`; resolve mirrors the code so the
        # expectation holds on Windows too, where resolve prepends a drive letter
        assert _worktree_branches(mock_repo) == {
            "main": Path("/repo/main").resolve().as_posix(),
            "refactor/dotbot": Path("/repo/feat").resolve().as_posix(),
        }


def _init_repo_with_commit(path: Path) -> Repo:
    """Create a git repo with one commit, usable as a worktree base."""
    actor = Actor("Test", "test@example.com")
    repo = Repo.init(path)
    (path / "README.md").write_text("init\n")
    repo.index.add(["README.md"])
    repo.index.commit("init", author=actor, committer=actor)
    return repo


class TestWorktreeGrouping:
    """Test that worktrees of one repo are grouped under their main worktree."""

    def test_linked_worktree_nested_under_main(self, tmp_path: Path) -> None:
        """A linked worktree is nested, not reported as a sibling repo."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        (tmp_path / "repo" / "README.md").write_text("changed\n")
        repo.git.stash("push", "-m", "wip")
        repo.git.worktree("add", str(tmp_path / "repo-wt"), "-b", "wt-branch")
        (tmp_path / "repo-wt" / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(tmp_path, recurse=1)

        # the linked worktree does not appear as its own top-level entry
        assert "repo-wt" not in result
        # it is nested under the main worktree instead
        worktrees = result["repo"]["worktrees"]
        assert isinstance(worktrees, dict)
        assert "repo-wt" in worktrees

    def test_shared_state_reported_once_on_main(self, tmp_path: Path) -> None:
        """Repo-level state lives on the main worktree, not the nested one."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        (tmp_path / "repo" / "README.md").write_text("changed\n")
        repo.git.stash("push", "-m", "wip")
        repo.git.worktree("add", str(tmp_path / "repo-wt"), "-b", "wt-branch")
        (tmp_path / "repo-wt" / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(tmp_path, recurse=1)

        main = result["repo"]
        worktrees = main["worktrees"]
        assert isinstance(worktrees, dict)
        nested = worktrees["repo-wt"]
        assert isinstance(nested, dict)
        # the stash is shared: it shows on main, never duplicated in the worktree
        assert main["stash_count"] == 1
        assert "stash_count" not in nested
        # working-tree state is reported on the worktree itself
        assert nested["untracked_files"] == ["dirty.txt"]

    def test_worktrees_sorted_alphabetically(self, tmp_path: Path) -> None:
        """Nested worktrees are ordered by name regardless of scan order."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        for name in ("repo-charlie", "repo-alpha", "repo-bravo"):
            repo.git.worktree("add", str(tmp_path / name), "-b", f"b-{name}")
            (tmp_path / name / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(tmp_path, recurse=1)

        worktrees = result["repo"]["worktrees"]
        assert isinstance(worktrees, dict)
        assert list(worktrees) == ["repo-alpha", "repo-bravo", "repo-charlie"]

    def test_plain_repo_not_nested(self, tmp_path: Path) -> None:
        """A repo with no linked worktree stays flat (no worktrees key)."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        (tmp_path / "repo" / "README.md").write_text("changed\n")
        repo.git.stash("push", "-m", "wip")

        result = issues_for_all_subfolders(tmp_path, recurse=1)

        assert "worktrees" not in result["repo"]
        assert result["repo"]["stash_count"] == 1

    def test_unscanned_main_is_marked(self, tmp_path: Path) -> None:
        """When the main worktree is outside the scan, it is flagged."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        scan = tmp_path / "scan"
        scan.mkdir()
        repo.git.worktree("add", str(scan / "wt"), "-b", "wt-branch")
        (scan / "wt" / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(scan, recurse=1)

        # the main repo was not scanned, so it is hosted as an external entry
        external = next(v for v in result.values() if v.get("main_worktree_unscanned"))
        worktrees = external["worktrees"]
        assert isinstance(worktrees, dict)
        assert "wt" in worktrees

    def test_branch_names_its_worktree_relative_to_scan(self, tmp_path: Path) -> None:
        """A diverged branch names the worktree holding it, keyed like the report."""
        repo = _init_repo_with_commit(tmp_path / "repo")
        repo.git.worktree("add", str(tmp_path / "repo-wt"), "-b", "wt-branch")
        # an unpushed commit on the branch checked out in the linked worktree
        wt = Repo(tmp_path / "repo-wt")
        (tmp_path / "repo-wt" / "f.txt").write_text("x")
        wt.index.add(["f.txt"])
        wt.index.commit(
            "work", author=wt.head.commit.author, committer=wt.head.commit.author
        )

        result = issues_for_all_subfolders(tmp_path, recurse=1)

        branches = result["repo"]["branches"]
        assert isinstance(branches, dict)
        wt_branch = branches["wt-branch"]
        assert isinstance(wt_branch, dict)
        # the worktree key matches how the worktree itself is keyed in the report
        assert wt_branch["worktree"] == "repo-wt"

    def test_external_worktree_ignored_by_default(self, tmp_path: Path) -> None:
        """A worktree outside the scan is not analyzed without the flag."""
        repo = _init_repo_with_commit(tmp_path / "scan" / "repo")
        repo.git.worktree("add", str(tmp_path / "outside-wt"), "-b", "wt-branch")
        (tmp_path / "outside-wt" / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(tmp_path / "scan", recurse=1)

        assert "worktrees" not in result.get("repo", {})

    def test_external_worktree_scanned_with_flag(self, tmp_path: Path) -> None:
        """With the flag, a worktree outside the scan is analyzed and nested."""
        repo = _init_repo_with_commit(tmp_path / "scan" / "repo")
        repo.git.worktree("add", str(tmp_path / "outside-wt"), "-b", "wt-branch")
        (tmp_path / "outside-wt" / "dirty.txt").write_text("x")

        result = issues_for_all_subfolders(
            tmp_path / "scan", recurse=1, scan_external_worktrees=True
        )

        worktrees = result["repo"]["worktrees"]
        assert isinstance(worktrees, dict)
        # the external worktree is keyed by its path relative to the scan base
        key = next(iter(worktrees))
        assert key.endswith("outside-wt")
        assert worktrees[key] == {"untracked_files": ["dirty.txt"]}
