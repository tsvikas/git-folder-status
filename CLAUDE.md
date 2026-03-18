# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

This project uses **uv** and **just** for development workflow:

```bash
# Setup development environment (run after cloning)
uv run just prepare

# Code quality
uv run just format           # Format code (isort + black + docs formatting)
uv run just lint             # Run ruff + mypy + deptry + pre-commit
uv run just test             # Run pytest with coverage
uv run just quick-tools      # Fast formatting + linting (alias: q)

# Combined workflows
uv run just check-and-push   # Assert clean repo + test + lint, then git push

# Documentation
uv run just build-docs       # Build MkDocs documentation
uv run just serve-docs       # Serve docs locally

# Other tasks
uv run just deps-update      # Update all dependencies
uv run just test-lowest 3.10 # Test with lowest dependency versions
```

**CRITICAL**: Always run `uv run just format` then `uv run just lint` and `uv run just test` before committing.

- If formatting changes files, `git add` the changes and re-commit.
- If a tool finds an issue, fix it and re-run just that tool.
- Finish with a full `format`, `lint`, `test` cycle.

## Architecture

**git-folder-status** is a Python CLI tool that recursively scans directories for Git repositories with uncommitted changes, unpushed commits, and other status issues.

### Core Components

- **Main Logic**: `src/git_folder_status/git_folder_status.py` - Contains all Git analysis logic using GitPython
- **CLI Interface**: `src/git_folder_status/cli.py` - Cyclopts-based CLI with comprehensive options
- **Entry Point**: Installable as `git-folder-status` command

### Key Design Patterns

- **Output Formats**: Supports YAML, JSON, colored reports, and Python pprint
- **Performance Options**: `--slow` flag enables expensive operations (tag checking)
- **Recursion Control**: Configurable depth limiting and directory exclusion
- **Error Handling**: Graceful handling of broken repositories and permission issues

## Code Quality Standards

This project enforces strict quality standards through `just format`, `just lint`, and `just test`.

### Quality Tools

- **MyPy**: Strict mode with extensive error codes enabled
- **Black**: Code formatting with docstring code formatting
- **Ruff**: ALL rules enabled with specific exceptions in pyproject.toml
- **pytest**: With coverage, strict markers, and doctest integration
- **Pre-commit**: 50+ hooks including security, spell checking, file validation

### Requirements

- All functions must have type hints
- Branch coverage required for all new code
- Tests run across Python 3.10-3.14 including PyPy variants
- All pre-commit hooks must pass

## Development Environment Notes

### Dependencies

- **uv** (>=0.5.19) is the primary package manager
  - use `uv add/remove` to add/remove dependencies
  - use `uv sync` to install dependencies
- **GitPython** for repository operations
- **Cyclopts** for CLI interface
- Supports Python 3.10+ including PyPy variants

### Template-based Project

This project is based on `tsvikas/python-template` v0.19.1. Core configuration is in:

- `pyproject.toml` - Primary project configuration
- `justfile` - Task definitions
- `.copier-answers.yml` - Template configuration tracking

## Common Development Tasks

### Adding New Features

1. Implement in `src/git_folder_status/git_folder_status.py` or create new modules
1. Add CLI options in `src/git_folder_status/cli.py` if needed
1. Add comprehensive tests in `tests/`. Use `uv run pytest` to check your tests.
1. Update type hints and ensure MyPy passes
1. **MANDATORY**: Run `uv run just format` then `uv run just lint` and `uv run just test` before committing
1. If formatting changes files during step 5, `git add` the changes and re-commit

### Performance Considerations

- Aim for quick Git code. Use `--slow` only if no other option works
- The code uses async to parallelize
