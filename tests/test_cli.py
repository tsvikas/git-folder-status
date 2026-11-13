import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from git_folder_status import DEFAULT_JOBS, RepoStats, __version__, cli
from git_folder_status.cli import (
    EX_NOINPUT,
    EX_NOPERM,
    EX_SOFTWARE,
    EX_UNAVAILABLE,
    app,
    main,
)


@pytest.fixture(autouse=True)
def _exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI's immediate exits raise `SystemExit`, so pytest lives on."""

    def interrupted() -> NoReturn:
        raise SystemExit(128 + signal.SIGINT)

    monkeypatch.setattr(cli, "_exit", sys.exit)
    monkeypatch.setattr(cli, "_exit_interrupted", interrupted)


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


def test_jobs_reaches_the_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--jobs` sets how many repos the scan analyzes at once."""
    seen: list[int] = []

    def scan(*_args: object, jobs: int, **_kwargs: object) -> dict[str, RepoStats]:
        seen.append(jobs)
        return {}

    monkeypatch.setattr(cli, "issues_for_all_subfolders", scan)
    app([str(tmp_path)], result_action="return_value")
    app(["-j", "3", str(tmp_path)], result_action="return_value")
    assert seen == [DEFAULT_JOBS, 3]


def test_jobs_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--jobs", "0", str(tmp_path)])
    assert exc_info.value.code == 2


def test_invalid_format() -> None:
    """Test invalid format raises error."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--format", "invalid"])
    # Cyclopts >=5 exits 2 on its own parse errors.
    assert exc_info.value.code == 2


class _InterruptedError(Exception):
    """Raised in place of ending the test process on Ctrl-C."""


def test_main_interrupted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Ctrl-C during the scan reaches `main`, not cyclopts' `sys.exit(130)`."""

    def interrupt(*_args: object, **_kwargs: object) -> NoReturn:
        raise KeyboardInterrupt

    def interrupted() -> NoReturn:
        raise _InterruptedError

    monkeypatch.setattr(cli, "issues_for_all_subfolders", interrupt)
    monkeypatch.setattr(cli, "_exit_interrupted", interrupted)
    with pytest.raises(_InterruptedError):
        main([str(tmp_path)])


# Run in a child process: the real exits end the process that calls them. The
# child leaves a pool thread asleep, as a stopped scan leaves its analyses.
_EXIT_SCRIPT = """
import sys, time
from concurrent.futures import ThreadPoolExecutor
from git_folder_status import cli
ThreadPoolExecutor(1).submit(time.sleep, 30)
time.sleep(0.2)
getattr(cli, sys.argv[1])(*map(int, sys.argv[2:]))
"""


def _run_exit(*args: str) -> tuple[int, float]:
    start = time.monotonic()
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _EXIT_SCRIPT, *args], check=False, timeout=20
    )
    return done.returncode, time.monotonic() - start


def test_exit_does_not_wait_for_threads() -> None:
    code, seconds = _run_exit("_exit", str(EX_SOFTWARE))
    assert code == EX_SOFTWARE
    assert seconds < 10  # the pool thread sleeps 30s


def test_interrupted_dies_by_sigint() -> None:
    code, seconds = _run_exit("_exit_interrupted")
    expected = 128 + signal.SIGINT if sys.platform == "win32" else -signal.SIGINT
    assert code == expected
    assert seconds < 10


def test_flush_std_streams_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`>&-` leaves stdout None, and a closed pipe fails stderr's flush."""
    stderr = MagicMock()
    stderr.flush.side_effect = BrokenPipeError
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", stderr)
    cli._flush_std_streams()  # noqa: SLF001
    stderr.flush.assert_called_once()
