from unittest.mock import patch

from typer.testing import CliRunner

from git_folder_status import __version__
from git_folder_status.cli import app

runner = CliRunner()


def test_app() -> None:
    result = runner.invoke(app)
    assert result.exit_code == 0
    assert "" in result.stdout


def test_app_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_invalid_format() -> None:
    """Test invalid format raises error."""
    result = runner.invoke(app, ["--format", "invalid"])
    assert result.exit_code != 0
    # The error is caught by Typer and results in SystemExit
    assert isinstance(result.exception, SystemExit)


def test_module_not_found_error() -> None:
    """Test ModuleNotFoundError handling."""
    with patch("git_folder_status.cli.format_report") as mock_format:
        mock_format.side_effect = ModuleNotFoundError("test module not found")
        result = runner.invoke(app, [])
        assert result.exit_code != 0
        assert "Missing module for format" in str(result.exception)
