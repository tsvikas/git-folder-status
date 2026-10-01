import pytest

from git_folder_status import __version__, cli
from git_folder_status.cli import (
    EX_NOINPUT,
    EX_NOPERM,
    EX_SOFTWARE,
    EX_UNAVAILABLE,
    app,
    main,
)


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


def test_main_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--not-an-option"])
    # Cyclopts >=5 exits 2 on invalid usage, as argparse, click and clap do.
    # sysexits(3) would say 64, but 2 is the far wider convention.
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError("missing.txt"), EX_NOINPUT),
        (PermissionError("locked.txt"), EX_NOPERM),
        (ConnectionError("down"), EX_UNAVAILABLE),
        # a subclass lands on its parent's code
        (ConnectionRefusedError("refused"), EX_UNAVAILABLE),
    ],
)
def test_main_reported_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == code
    assert capsys.readouterr().err == f"error: {error}\n"


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
        main(["--format", "invalid"])
    # Cyclopts >=5 exits 2 on its own parse errors.
    assert exc_info.value.code == 2
