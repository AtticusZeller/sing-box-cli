# sing-box-cli (`sbc`)

A CLI that manages sing-box: subscriptions and configuration, the service (systemd on
Linux, a Go SCM helper on Windows, or the official sing-box desktop app's daemon), and
Clash API views (`stats`, `conns`, `proxy`, `logs`). Published on PyPI as `sing-box-cli`.

## Run

```bash
git fetch && git log --oneline HEAD..origin/main   # local checkouts are often behind
uv sync --all-extras --dev
uv run sbc --help
uv run bash scripts/pre-commit.sh   # what CI runs: format, mypy, ruff, tests + coverage, pre-commit
uv run mypy src --platform win32    # CI also runs on Windows
```

## Stack

Python 3.11–3.14, typer (rich), httpx, pydantic, plotext, sing-box-bin (the core),
SQLite (stdlib), optional `grpcio` (`[desktop]` extra), Go for `windows-service/`,
hatchling with a build hook (`hatch_build.py`) that compiles the Windows helper into
the wheel. `plotext<6` and `sing-box-bin<1.14` are deliberate caps (#31): do not merge
Dependabot PRs that lift them without checking why.

## Layout and conventions

- `src/sing_box_cli/config/`: `store.py` is the configuration model, the desktop
  app's layout: `settings.db` + `profiles/<id>.json`, used with or without the app.
  `config.py` (`ConfigHandler`) sits on top of it. `serve*.py`: `sbc config serve`.
- `src/sing_box_cli/service/`: `manager.py` (systemd, Windows helper), `desktop.py`
  (the desktop app's daemon over its gRPC socket; grpc is imported lazily).
- The desktop app counts as installed when `sing-box-daemon.service` exists. Then its
  store is only read and updated, never created or repaired; errors are reported, not
  worked around. `/usr/bin/sing-box` is then the app, not the core.
- Subscription tokens live inside the stored URL (`?token=`), are sent as a bearer
  token and are never printed or logged.
- Commits: Conventional Commits with a scope. PRs are squash-merged.
- `tests/api/test_monitor.py` gets reformatted by `scripts/format.sh`; that change is
  not part of any commit, revert it.

## Release

1. Add a curated `## unreleased` section at the top of `CHANGELOG.md`.
2. `uv run bump-my-version bump patch --no-commit --no-tag`; this updates
   `__init__.py`, `pyproject.toml` and the changelog heading.
3. Commit `chore(release): X → Y`, create the annotated tag `vY` with the same message,
   then push `main` and the tag. The tag's CI publishes to PyPI and the GitHub release.

Building locally from the sdist (`uv build`) may need `GOFLAGS=-buildvcs=false`.

## State

0.1.9 (2026-10-14) added the desktop app mode and the settings.db store.
Not verified at runtime: the Windows service paths against a real SCM.
