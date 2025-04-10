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
