from unittest.mock import patch

import pytest

from git_folder_status import __version__
from git_folder_status.cli import app


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--version")
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_app() -> None:
    with pytest.raises(SystemExit) as exc_info:
        app([])
    assert exc_info.value.code == 0
    # TODO: convert to better tests -- test in a temp folder


def test_invalid_format() -> None:
    """Test invalid format raises error."""
    with pytest.raises(SystemExit) as exc_info:
        app(["--format", "invalid"])
    assert exc_info.value.code != 0


def test_module_not_found_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Test ModuleNotFoundError handling."""
    with patch("git_folder_status.cli.format_report") as mock_format:
        mock_format.side_effect = ModuleNotFoundError("test module not found")
        with pytest.raises(SystemExit) as exc_info:
            app([])
    assert exc_info.value.code != 0
    assert "Missing module for format" in str(capsys.readouterr().err)
