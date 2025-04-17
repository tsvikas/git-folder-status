# git-folder-status

[![Tests][tests-badge]][tests-link]
[![uv][uv-badge]][uv-link]
[![Ruff][ruff-badge]][ruff-link]
[![Black][black-badge]][black-link]
\
[![Made Using tsvikas/python-template][template-badge]][template-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]
[![PRs Welcome][prs-welcome-badge]][prs-welcome-link]

## Overview

Find all subdirectories with uncommitted or unpushed code.

![Screenshot](assets/screenshot.png)

## Usage

Install the package using pipx/uv:

```bash
uv tool install git+https://github.com/tsvikas/git-folder-status.git`
```

<!---
# TODO: replace with this after uploading to PyPI:
uv tool install git-folder-status
-->

and run it:

```bash
git-folder-status /path/to/code/directory
```

## Development

### Getting started

- install [git][install-git], [uv][install-uv].
- git clone this repo:
  `git clone https://github.com/tsvikas/git-folder-status.git`
  or `gh repo clone tsvikas/git-folder-status.git`
- run `uv run just prepare`

### Tests and code quality

- use `uv run just format` to format the code.
- use `uv run just lint` to see linting errors.
- use `uv run just test` to run tests.
- use `uv run just check` to run all the checks (format, lint, test, and pre-commit).
- Run a specific tool directly, with
  `uv run pytest`/`ruff`/`mypy`/`black`/`pre-commit`/...

<!--- Badges to add after adding RTD documentation
[![Documentation Status][rtd-badge]][rtd-link]

[rtd-badge]: https://readthedocs.org/projects/git-folder-status/badge/?version=latest
[rtd-link]: https://git-folder-status.readthedocs.io/en/latest/?badge=latest
-->

<!--- Badges to add after writing tests. Also follow the link to finish setup
[![codecov][codecov-badge]][codecov-link]

[codecov-badge]: https://codecov.io/gh/tsvikas/git-folder-status/graph/badge.svg
[codecov-link]: https://codecov.io/gh/tsvikas/git-folder-status
-->

<!--- Badges to add after uploading to PyPI
[![PyPI version][pypi-version-badge]][pypi-link]
[![PyPI platforms][pypi-platforms-badge]][pypi-link]
[![Total downloads][pepy-badge]][pepy-link]

[pepy-badge]: https://img.shields.io/pepy/dt/git-folder-status
[pepy-link]: https://pepy.tech/project/git-folder-status
[pypi-link]: https://pypi.org/project/git-folder-status/
[pypi-platforms-badge]: https://img.shields.io/pypi/pyversions/git-folder-status
[pypi-version-badge]: https://img.shields.io/pypi/v/git-folder-status
-->

[black-badge]: https://img.shields.io/badge/code%20style-black-000000.svg
[black-link]: https://github.com/psf/black
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]: https://github.com/tsvikas/git-folder-status/discussions
[install-git]: https://git-scm.com/book/en/v2/Getting-Started-Installing-Git
[install-uv]: https://docs.astral.sh/uv/getting-started/installation/
[prs-welcome-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg
[prs-welcome-link]: http://makeapullrequest.com
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-link]: https://github.com/astral-sh/ruff
[template-badge]: https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold
[template-link]: https://github.com/tsvikas/python-template
[tests-badge]: https://github.com/tsvikas/git-folder-status/actions/workflows/ci.yml/badge.svg
[tests-link]: https://github.com/tsvikas/git-folder-status/actions/workflows/ci.yml
[uv-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json
[uv-link]: https://github.com/astral-sh/uv
