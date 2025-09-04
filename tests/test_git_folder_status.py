"""Tests for git_folder_status module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import pytest
from git import Repo

from git_folder_status.git_folder_status import (
    RepoStats,
    branch_status,
    format_report,
    is_file,
    issues_for_all_subfolders,
    issues_for_one_folder,
    repo_issues_in_branches,
    repo_issues_in_stats,
    repo_issues_in_tags,
    repo_stats,
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


class TestRepoStats:
    """Test repo_stats function."""

    def test_repo_stats_normal(self) -> None:
        """Test repo_stats with normal repo."""
        mock_repo = Mock(spec=Repo)
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

            result = repo_issues_in_stats(mock_repo, slow=False, include_all=False)

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

            result = repo_issues_in_stats(mock_repo, slow=False, include_all=True)

            # With include_all=True, should include more fields
            assert "branches" in result


class TestBranchStatus:
    """Test branch_status function."""

    def test_no_tracking_branch(self) -> None:
        """Test branch with no tracking branch."""
        mock_repo = Mock(spec=Repo)
        mock_branch = Mock()
        mock_branch.tracking_branch.return_value = None

        result = branch_status(mock_repo, mock_branch)
        assert result == {"remote_branch": False}

    def test_local_tracking_branch(self) -> None:
        """Test branch tracking a local branch."""
        mock_repo = Mock(spec=Repo)
        mock_branch = Mock()
        mock_tracking = Mock()
        mock_tracking.name = ".local_branch"
        mock_branch.tracking_branch.return_value = mock_tracking

        result = branch_status(mock_repo, mock_branch)
        assert result == {"remote_branch": False}

    def test_missing_remote_branch(self) -> None:
        """Test branch tracking a missing remote branch."""
        mock_repo = Mock(spec=Repo)
        mock_branch = Mock()
        mock_branch.name = "feature"
        mock_tracking = Mock()
        mock_tracking.name = "origin/feature"
        mock_branch.tracking_branch.return_value = mock_tracking

        mock_repo.refs = {}  # Remote branch doesn't exist

        result = branch_status(mock_repo, mock_branch)
        assert result == {
            "remote_branch": "origin/feature",
            "remote_branch_exists": False,
        }


class TestRepoIssuesInBranches:
    """Test repo_issues_in_branches function."""

    def test_branches_without_remote(self) -> None:
        """Test detection of branches without remote."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "remote_branch": "origin/main",
                    "commits_ahead": 0,
                    "commits_behind": 0,
                },
                "feature": {"remote_branch": False},
            }

            result = repo_issues_in_branches(mock_repo, slow=False, include_all=False)

            assert result["branches_without_remote"] == ["feature"]

    def test_include_all_branches(self) -> None:
        """Test include_all flag includes synced branches."""
        mock_repo = Mock(spec=Repo)

        with patch(
            "git_folder_status.git_folder_status.all_branches_status"
        ) as mock_all_branches:
            mock_all_branches.return_value = {
                "main": {
                    "remote_branch": "origin/main",
                    "commits_ahead": 0,
                    "commits_behind": 0,
                    "remote_branch_exists": True,
                },
            }

            result = repo_issues_in_branches(mock_repo, slow=False, include_all=True)

            assert "branches" in result


class TestRepoIssuesInTags:
    """Test repo_issues_in_tags function."""

    def test_include_all_local_tags(self) -> None:
        """Test include_all flag includes local tags."""
        mock_repo = Mock(spec=Repo)
        mock_tag = Mock()
        mock_tag.path = "refs/tags/v1.0.0"
        mock_tag.commit.hexsha = "abc123"
        mock_repo.tags = [mock_tag]

        result = repo_issues_in_tags(mock_repo, slow=False, include_all=True)

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

        result = repo_issues_in_tags(mock_repo, slow=True, include_all=False)

        # Should detect mismatch between local and remote tags
        assert "tags_mismatch" in result


class TestIssuesForOneFolder:
    """Test issues_for_one_folder function."""

    def test_invalid_git_repository(self) -> None:
        """Test handling of non-git directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)

            # Create some files to make it non-empty
            (folder / "file.txt").write_text("content")

            result = issues_for_one_folder(folder, slow=False, include_all=False)
            assert result == {"is_git": False}

    def test_empty_non_git_directory(self) -> None:
        """Test handling of empty non-git directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)

            result = issues_for_one_folder(folder, slow=False, include_all=False)
            assert result == {}

    def test_repository_error_handling(self) -> None:
        """Test error handling for repository analysis."""
        folder = Path("/nonexistent")

        with pytest.raises(RuntimeError, match="Error while analyzing repo"):
            issues_for_one_folder(folder, slow=False, include_all=False)


class TestIsFile:
    """Test is_file function."""

    def test_normal_file(self) -> None:
        """Test normal file detection."""
        with tempfile.NamedTemporaryFile() as temp_file:
            path = Path(temp_file.name)
            assert is_file(path) is True

    def test_directory(self) -> None:
        """Test directory is not a file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            assert is_file(path) is False

    def test_broken_symlink(self) -> None:
        """Test broken symlink handling."""
        with tempfile.TemporaryDirectory() as temp_dir:
            link_path = Path(temp_dir) / "broken_link"
            link_path.symlink_to("nonexistent_target")

            # Should return False for broken symlinks without raising
            assert is_file(link_path) is False


class TestFormatReport:
    """Test format_report function."""

    def test_yaml_format(self) -> None:
        """Test YAML format output."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}}
        result = format_report(issues, include_ok=True, fmt="yaml")
        assert "repo1:" in result
        assert "is_dirty: true" in result

    def test_json_format(self) -> None:
        """Test JSON format output."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}}
        result = format_report(issues, include_ok=True, fmt="json")
        parsed = json.loads(result)
        assert parsed == issues

    def test_pprint_format(self) -> None:
        """Test pprint format output."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}}
        result = format_report(issues, include_ok=True, fmt="pprint")
        assert "repo1" in result
        assert "is_dirty" in result

    def test_report_format_empty(self) -> None:
        """Test report format with empty issues."""
        result = format_report({}, include_ok=True, fmt="report")
        assert result == ""

    def test_report_format_with_issues(self) -> None:
        """Test report format with colored output."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}}
        result = format_report(issues, include_ok=True, fmt="report")
        assert "\033[91m" in result  # Red color code
        assert "\033[0m" in result  # Normal color code

    def test_include_ok_false(self) -> None:
        """Test include_ok=False filters empty issues."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}, "repo2": {}}
        result = format_report(issues, include_ok=False, fmt="json")
        parsed = json.loads(result)
        assert "repo1" in parsed
        assert "repo2" not in parsed

    def test_invalid_format_raises_error(self) -> None:
        """Test invalid format raises ValueError."""
        issues: dict[str, RepoStats] = {"repo1": {"is_dirty": True}}
        with pytest.raises(ValueError, match="format_report got an unsupported"):
            format_report(issues, include_ok=True, fmt="invalid")  # type: ignore[arg-type]


class TestIssuesForAllSubfolders:
    """Test issues_for_all_subfolders function."""

    def test_exclude_dirs(self) -> None:
        """Test directory exclusion."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create directories
            (basedir / "included").mkdir()
            (basedir / "excluded").mkdir()
            (basedir / ".hidden").mkdir()

            # Add files to make them detectable
            (basedir / "included" / "file.txt").write_text("content")
            (basedir / "excluded" / "file.txt").write_text("content")

            result = issues_for_all_subfolders(
                basedir,
                recurse=1,
                exclude_dirs=["excluded"],
                slow=False,
                include_all=False,
            )

            assert "included" in result
            assert "excluded" not in result
            assert ".hidden" not in result

    def test_broken_symlink_handling(self) -> None:
        """Test handling of broken symlinks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create broken symlink
            broken_link = basedir / "broken_link"
            broken_link.symlink_to("nonexistent_target")

            result = issues_for_all_subfolders(
                basedir, recurse=1, slow=False, include_all=False
            )

            assert "broken_link" in result
            assert result["broken_link"]["broken_link"] == "nonexistent_target"

    def test_recursive_symlink_avoidance(self) -> None:
        """Test avoidance of recursive symlinks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create subdirectory and symlink back to parent
            subdir = basedir / "subdir"
            subdir.mkdir()
            recursive_link = subdir / "parent_link"
            recursive_link.symlink_to(basedir)

            result = issues_for_all_subfolders(
                basedir, recurse=2, slow=False, include_all=False
            )

            # Should not include the recursive symlink
            assert "subdir" in result
            # The recursive link should be skipped, not cause infinite recursion

    @patch("sys.version_info", (3, 11, 0))
    def test_python_version_fallback(self) -> None:
        """Test fallback for Python < 3.12."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create a git repo in temp dir
            with patch("git_folder_status.git_folder_status.Repo") as mock_repo_class:
                mock_repo = Mock()
                mock_repo.working_tree_dir = str(basedir)
                mock_repo_class.return_value.__enter__.return_value = mock_repo

                with patch(
                    "git_folder_status.git_folder_status.issues_for_one_folder"
                ) as mock_issues:
                    mock_issues.return_value = {"is_dirty": True}

                    result = issues_for_all_subfolders(
                        basedir, recurse=1, slow=False, include_all=False
                    )

                    assert "<this repos>" in result

    def test_non_git_directory_with_files_and_symlinks(self) -> None:
        """Test handling of non-git directory with files and symlinks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create subdirectory with git repos and non-git content
            subdir = basedir / "subdir"
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

                def side_effect(folder: Path, **_kwargs: bool) -> RepoStats:
                    if folder.name == "git_repo":
                        return {"is_dirty": True}
                    return {"is_git": False}

                mock_issues.side_effect = side_effect

                result = issues_for_all_subfolders(
                    basedir, recurse=2, slow=False, include_all=False
                )

                # Should include both the git repo and note the non-git parent
                assert any("git_repo" in str(k) for k in result)
                assert "subdir" in result
                assert result["subdir"]["is_git"] is False
                assert "untracked_files" in result["subdir"]
                assert "sym_links" in result["subdir"]

    def test_directory_with_only_empty_subdirs(self) -> None:
        """Test directory with only empty subdirectories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            basedir = Path(temp_dir)

            # Create empty subdirectory
            empty_subdir = basedir / "empty_subdir"
            empty_subdir.mkdir()

            with patch(
                "git_folder_status.git_folder_status.issues_for_one_folder"
            ) as mock_issues:
                mock_issues.return_value = {"is_git": False}

                result = issues_for_all_subfolders(
                    basedir, recurse=1, slow=False, include_all=False
                )

                # Should mark the empty directory as not git
                assert "empty_subdir" in result
                assert result["empty_subdir"]["is_git"] is False
