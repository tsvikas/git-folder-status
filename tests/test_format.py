"""Tests for git_folder_status module."""

import json

import pytest
from colorama import Fore

from git_folder_status.format import (
    format_report,
)
from git_folder_status.git_folder_status import RepoStats


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
        assert Fore.LIGHTRED_EX in result
        assert Fore.RESET in result

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
