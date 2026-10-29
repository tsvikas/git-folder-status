"""Shared test setup."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run git without the developer's global and system config.

    Settings such as `diff.ignoreSubmodules` or `tag.gpgSign` change what git
    reports or does, so results would depend on whose machine runs the tests.
    Commands that create commits (stash, merge) still need an identity.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
