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

This script scans through a directory recursively to identify the status of
all Git repositories found within.

It returns a list of directories with their issues, including:

- directories with any content that are not part of a repo.
- uncommitted changes
- untracked files
- stash entries
- detached head
- branches without remote
- branches with unpushed changes
- tags without a remote tag (only with -s/--slow)
- tags that differ from their remote tag (only with -s/--slow)
- broken links outside repos

## Install

Install this tool using pipx (or uv):

```bash
pipx install git+https://github.com/tsvikas/git-folder-status.git
```

## Usage

Run it with:

```bash
git-folder-status /path/to/code/directory
```

Use `git-folder-status --help` to learn more.

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
