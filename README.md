<p align="center">
  <img src="./assets/readme/hero.svg" width="100%"
       alt="git-folder-status: scans a directory tree and reports every Git repo that is not fully synced with its remote. A folder tree tags my-repo as dirty, my-other-repo as ahead 1, tools/scanner as stash 1, notes as no remote, and dotfiles as clean.">
</p>

<p align="center">
<a href="https://github.com/tsvikas/git-folder-status/actions/workflows/ci.yml"><img src="https://github.com/tsvikas/git-folder-status/actions/workflows/ci.yml/badge.svg" alt="Tests"></a>
<a href="https://codecov.io/gh/tsvikas/git-folder-status"><img src="https://codecov.io/gh/tsvikas/git-folder-status/graph/badge.svg" alt="codecov"></a>
<a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
<a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
<a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Black"></a>
<br>
<a href="https://github.com/tsvikas/python-template"><img src="https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold" alt="Made Using tsvikas/python-template"></a>
<a href="https://github.com/tsvikas/git-folder-status/discussions"><img src="https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github" alt="GitHub Discussion"></a>
<a href="https://opensource.guide/how-to-contribute/"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>

## What it looks like

![One command scans every repo under a directory and prints only the ones with unsaved work](assets/screenshot.png)

Point it at the folder where you keep your projects.
It walks the tree, inspects every Git repo it finds, and prints only the ones holding work that is not safely on a remote.

## Install

```bash
pipx install git+https://github.com/tsvikas/git-folder-status.git
```

`uv tool install` works the same way.
Requires Python 3.10 or newer.

## Use

```bash
git-folder-status ~/code
```

By default it recurses 3 levels deep and prints a colored report of repos with issues.

| Option                 | Short | Does                                                   |
| ---------------------- | ----- | ------------------------------------------------------ |
| `--recurse N`          | `-r`  | Max depth to descend into directories (default 3)      |
| `--exclude-dir NAME`   | `-d`  | Skip these directories. Repeatable                     |
| `--format FMT`         | `-f`  | Output as `report`, `yaml`, `json`, or `pprint`        |
| `--empty`              | `-e`  | Also list repos that have no issues                    |
| `--all`                | `-a`  | Show extra info for each repo, not just issues         |
| `--slow`               | `-s`  | Enable expensive checks, currently tag comparison      |
| `--include-behind`     | `-b`  | Also flag branches that are only behind the remote     |
| `--external-worktrees` | `-w`  | Analyze worktrees living outside the scanned directory |

Use `--format json` or `--format yaml` to pipe the results into another tool.

## Why not just `git status`?

`git status` covers one repo, and only its working tree.
This tool covers a whole tree of repos, and catches states that `git status` stays quiet about:

- Uncommitted changes and untracked files
- Stash entries you forgot about
- Detached HEAD
- Branches with no remote at all
- Branches ahead of or behind their remote
- Tags missing from the remote, or differing from it (`--slow`)
- Non-repo directories that still contain files
- Broken symlinks

Linked worktrees of the same repo are grouped under their main worktree.
Repo-level state such as stashes, branches, and tags is reported once instead of repeated for every worktree.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

Licensed under the MIT License.
