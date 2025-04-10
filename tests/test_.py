import importlib

import git_folder_status


def test_version() -> None:
    assert importlib.metadata.version("git_folder_status") == git_folder_status.__version__
