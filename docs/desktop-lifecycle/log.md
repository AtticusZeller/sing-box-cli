# Desktop lifecycle implementation log

# Linux desktop lifecycle — issue #44

## Complete daemon and core lifecycle through enable/disable

Source: https://github.com/AtticusZeller/sing-box-cli/issues/44 (read 2026-10-25).
Status: implementation and local verification completed on 2026-10-25.
At the initial implementation handoff, the affected-machine check remained
pending and no commit, push or release had been performed. The user subsequently
authorized committing and publishing on 2026-10-25 and will run the manual check.

The issue defines the command contract: Linux desktop `enable` starts the daemon
and selected core, while `disable` stops both. `restart` continues hot reload;
`stop` stops only the core. Autostart policy, Windows behavior, standalone behavior,
desktop store ownership and foreground debug behavior remain unchanged.

Evidence: desktop.py currently prints autostart notices in create_service/disable;
service/__init__.py calls stop before disable, so RPC failure skips daemon shutdown.
debug.py already implements bounded non-blocking systemd stop, verified cgroup
TERM/KILL and final stop-job/process verification. Reuse this mechanism rather than
introduce broad process-name killing or a competing standalone unit.

### Task: Start the daemon before starting the selected core

**Change**

- [x] Introduce a backend lifecycle entry point for enable; its default preserves
  create_service + start. Override only Linux desktop; do not make create_service
  start the daemon because restart/config update also use that method.
- [x] Require root for desktop full lifecycle operations, retain invoking-user
  identity for profile/preferences and RPC. Validate selected configuration first.
- [x] Inspect the installed daemon unit with bounded systemctl; report missing,
  failed and inaccessible units explicitly. If inactive, print the startup stage
  and submit a non-blocking start. If already running, reuse it.
- [x] Wait at most 15 seconds for GetDaemonInfo RPC readiness, using per-call
  timeouts capped by the remaining deadline. Preserve ownership/dependency errors;
  distinguish daemon startup failure from a running daemon whose RPC is unavailable.
- [x] Start the selected profile with existing start options after readiness;
  print success only after the start RPC succeeds. Linux missing-daemon guidance
  points to sudo sbc service enable.

**Verification**

1. [x] Regression tests cover inactive/active daemon, missing unit, systemctl
   failure/timeout, permission failure, startup failure, readiness timeout, owner
   rejection and selected-profile start failure; assert ordering and exit codes.
2. [x] Assert restart never starts a stopped daemon and stop only calls core RPC.

**Done**

- [x] Recorded calls show daemon start (only when needed), RPC readiness under the
  invoking user, then selected-profile core start; all failed stages suppress success.

### Task: Stop the daemon even when core RPC fails

**Change**

- [x] Introduce a backend lifecycle entry point for full disable; its default
  preserves stop + disable and current standalone/Windows output. Linux desktop
  requests core stop and polls core status until Stopped before stopping daemon.
  This ordering was explicitly clarified and approved by the user. If RPC fails
  or core-stop verification times out, explicitly say the core state could not be
  confirmed through RPC, then continue system-level daemon shutdown and verify its
  entire process group is empty. Core RPC work shares a 15-second deadline.
- [x] Parameterize the existing verified systemd stop mechanism to allow stopping
  only sing-box-daemon.service. Debug takeover retains both units and GUI shutdown;
  normal disable does not kill the GUI or unrelated standalone service.
- [x] Keep non-blocking stop submission, verified cgroup process identity, bounded
  TERM/KILL and final unit/job/cgroup checks. Submission or verification failure
  returns nonzero. If RPC failed but verified daemon shutdown succeeded, report
  recovered full shutdown accurately; do not report autostart changes.

**Verification**

1. [x] Tests cover healthy core-first order, absent daemon, hung/erroring RPC with
   successful fallback, stuck systemctl/stop jobs, surviving processes and permission
   failure. Assert no false success and no GUI/standalone process targeting.
2. [x] Rerun existing debug takeover, Windows desktop and standalone command tests.

**Done**

- [x] RPC failure cannot bypass the daemon stop attempt. Only verified daemon
  shutdown produces full-stop success, and no autostart setting is changed.

### Task: Review, validate and document the behavior

**Change**

- [x] Obtain an independent agent review against this plan and verify findings.
- [x] Update README and lifecycle overview/log with command semantics, time bounds,
  recovery instructions and verified limits. Add a curated unreleased changelog.
- [x] Leave commits, pushes and release for an explicit user request in this task
  (received on 2026-10-25 after the implementation handoff).

**Verification**

1. [x] Run relevant regression tests, changed-file lint/type checks, repository
   pre-commit script and Windows-target mypy; discard unrelated formatter noise in
   tests/api/test_monitor.py only.
2. [ ] Real desktop check (user-run; interrupts networking): sudo sbc service disable,
   confirm daemon inactive/no service cgroup processes; sudo sbc service enable,
   confirm selected profile and functioning proxy; repeat enable while running.
   Also reproduce disable with unresponsive RPC, then enable recovery. Capture
   output, exit codes and systemd state; simulations alone do not complete this check.

**Done**

- [x] Automated checks and independent review provide evidence for implemented
  behavior; actual desktop lifecycle/TUN recovery remains explicitly pending until
  observed on the affected machine. No host service is interrupted during coding.

### Evidence and implementation corrections

- Initial regression run against the old behavior: 6 failed, 13 passed. It exposed
  missing daemon startup/shutdown, absent post-stop core verification and false
  autostart reporting.
- Reused debug._stop_units with an optional explicit unit tuple. Its default must
  resolve _UNITS at call time: binding the tuple in the function signature broke
  the existing controlled-child test that changes the selected units. Corrected
  that binding; the debug regression suite passed afterward.
- The first full run found one old test whose unconstrained MagicMock swallowed
  the new enable_service entry point (445 passed, 1 failed). The test now uses the
  real base lifecycle method with mocked create/start, retaining its original
  start-failure/no-success assertion. Default backend ordering/output also has
  explicit coverage.
- Final `uv run bash scripts/pre-commit.sh`: 448 passed, 87% coverage; mypy,
  ruff and all pre-commit hooks passed. `uv run mypy src --platform win32`:
  24 source files passed. Independent reviewer ran all then-current service tests
  (167 passed), found no actionable findings, and separately approved the final
  test adjustments and README/overview.
- RPC tests use a local gRPC Unix-socket server, including actual client deadlines
  against handlers that withhold replies. Systemd and cgroups are simulated;
  existing debug tests include a controlled real child ignoring TERM. No actual
  sing-box daemon, GUI or network service was stopped during this work.
- README, AGENTS.md, curated unreleased changelog and overview match the code.
  The only runtime claim left open is actual desktop/TUN recovery. Memory is
  generated/read-only and was not modified. Existing unrelated worktrees were
  not touched. Formatter-only changes to tests/api/test_monitor.py were reverted.
