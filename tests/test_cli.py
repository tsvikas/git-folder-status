from unittest.mock import patch

import pytest

from git_folder_status import __version__, cli
from git_folder_status.cli import EX_SOFTWARE, EX_USAGE, app, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--version")
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_app() -> None:
    with pytest.raises(SystemExit) as exc_info:
        app([])
    assert exc_info.value.code == 0
    # TODO: convert to better tests -- test in a temp folder<<<<<<< before updating


def test_main_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--not-an-option"])
    assert exc_info.value.code == EX_USAGE


def test_main_unhandled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Accept the call that `main` makes, so that the RuntimeError below is what
    # reaches it, rather than a TypeError over the signature.
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == EX_SOFTWARE


def test_invalid_format() -> None:
    """Test invalid format raises error."""
    with pytest.raises(SystemExit) as exc_info:
        app(["--format", "invalid"])
    assert exc_info.value.code == EX_USAGE


def test_module_not_found_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Test ModuleNotFoundError handling."""
    with patch("git_folder_status.cli.format_report") as mock_format:
        mock_format.side_effect = ModuleNotFoundError("test module not found")
        with pytest.raises(SystemExit) as exc_info:
            app([])
    assert exc_info.value.code != 0
    assert "Missing module for format" in str(capsys.readouterr().err)
