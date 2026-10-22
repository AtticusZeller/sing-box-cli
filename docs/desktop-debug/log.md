# Desktop debug fallback

Status: implementation and automated verification completed on 2026-10-22.
Real-machine validation is pending; see plan.md.

## Explicit Linux foreground takeover

The reported `sbc service status` failure is a desktop gRPC deadline, not evidence
of its underlying cause. `main.run` currently rejects all desktop installations.
Removing that guard alone is insufficient: `ConfigHandler.bin_path` prefers PATH,
where `/usr/bin/sing-box` may be the desktop application rather than the core.
The installed daemon unit also uses `Restart=always`; killing its PID alone would
allow systemd to restart it.

Use an explicit, invocation-local `sudo sbc run --force-cli` option. Keep normal
desktop service commands on their existing backend. This change targets Linux;
Windows takeover remains unsupported and must fail before changing any state.
Keep desktop ownership of settings.db and profiles even when execution is forced
through the CLI. Do not create, repair, or migrate desktop-owned settings.

### Task: Run the active desktop profile without a responsive daemon

**Change**

- [x] Add `run --force-cli`. With a desktop installation, ordinary `run` explains
  the explicit takeover command; without one, existing foreground behavior stays.
- [x] Validate privileges, platform, active profile, and bundled `sing-box-bin`
  core before stopping anything. Use that core explicitly for desktop fallback,
  never the desktop executable found on PATH. Forward its exit status.
- [x] Exit the invoking user's desktop application processes using verified
  executable identities and their associated process tree, not a broad
  `pkill -f sing-box`. Send TERM first, then KILL only for verified survivors after
  a bounded wait. Other users' GUI processes are outside scope.
- [x] Stop `sing-box-daemon.service` through systemd, including its service
  process group, without calling desktop gRPC. Stop an existing standalone
  `sing-box.service` too so it cannot compete for TUN, API ports, or cache.
  Bound waits and abort on an unverifiable or failed stop; never start the debug
  core while a competing managed core is still active.
- [x] Run the selected profile in the foreground with the existing config
  directory and inherited terminal output. Preserve `--update` and
  `--clear-cache`; stop competing cores before clearing cache and abort if an
  explicitly requested update fails.
- [x] Keep desktop and standalone services stopped after exit or Ctrl+C. Do not
  uninstall, disable autostart, or persist a backend preference. Print recovery
  instructions for starting the desktop daemon and reopening the application.
- [x] On Linux desktop RPC deadline/unavailability errors, retain the original
  error and add the explicit fallback command; never trigger takeover from status.

**Verification**

1. [x] Add regression tests for desktop refusal without the flag, fallback with
   an unresponsive daemon, bundled-core selection despite a GUI on PATH, and
   preservation of active-profile/store ownership.
2. [x] Test stop ordering, bounded termination, failure preventing core launch,
   current-user process boundaries, update/cache behavior, Ctrl+C and child exit
   status, plus rejection on Windows before side effects.
3. [x] Run relevant tests, changed-file lint/type checks, repository pre-commit
   checks, and Windows mypy checks. Revert only formatting introduced by the
   formatter in tests/api/test_monitor.py; preserve any pre-existing edits.
4. [x] Take an independent code review against this plan and resolve confirmed
   findings. Update README with usage, service interruption, recovery, and the
   Linux-only scope.
5. [ ] User runtime check on the affected machine: invoke
   `sudo sbc run --force-cli` while desktop RPC is unresponsive. Pass criteria:
   desktop GUI/daemon stop, the core prints foreground startup or configuration
   errors without a gRPC dependency, and Ctrl+C leaves no competing core running.
   This check is pending; mocks cannot establish real TUN/cache cleanup.

**Done**

- [x] A hung desktop RPC cannot prevent explicitly requested foreground debugging.
- [x] The executed binary is the independent core and the configuration is the
  selected desktop profile, with no replacement settings database.
- [x] Failed takeover never launches a competing core; normal status remains
  observational and normal desktop service routing remains unchanged.
- [x] Tests and review evidence are recorded, with real-machine validation marked
  pending unless it was actually performed. No release, commit, push, or local
  service interruption is included in this planning request.

### Verification evidence

- `uv sync --all-extras --dev`: completed.
- Relevant regression suite: 38 passed.
- `uv run bash scripts/pre-commit.sh`: passed, including all 397 tests,
  87% total coverage, Linux mypy, lint/format and tracked-file pre-commit checks.
- `uv run mypy src --platform win32`: passed for 24 source files.
- Independent read-only review against this plan: no confirmed defects;
  reviewer independently ran 36 desktop/debug tests and `git diff --check`.
- `uv run sbc run --help`: exposes the new flag. No service was interrupted.
- Reverted formatter-only changes to `tests/api/test_monitor.py` as required by
  AGENTS.md. No pre-existing user edits were present.
- Runtime evidence remains pending: fake systemd/process tests prove ordering and
  failure handling, not real daemon termination or TUN/cache release.
- At implementation handoff, publication and system installation were out of scope
  and changes were uncommitted. The user subsequently authorized release v0.1.13.
  Real-machine validation remains pending independently of publication.
- Release preparation: version/changelog updates and explicit-file pre-commit
  checks passed. Local wheel construction could not complete because Go was not
  available on PATH. The release workflow installs the Go version specified by
  windows-service/go.mod and verifies the packaged helper before publication.
