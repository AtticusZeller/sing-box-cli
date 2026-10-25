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

## Correct the blocking service-stop path

Status: correction confirmed, implemented, and verified locally on 2026-10-25.
Real-machine recovery remains pending. The user subsequently authorized release v0.1.14.

On 2026-10-25 the user reported that `sudo sbc run --force-cli` still blocks in
systemctl and that `sudo pkill -f sing-box` allowed recovery. The exact blocking
subcommand and daemon failure remain unverified. The v0.1.13 implementation used
synchronous `systemctl stop` with a 20-second subprocess timeout and aborts on
failure. Its TERM/KILL escalation applies to GUI processes only, not the daemon.
The earlier automated checks therefore did not establish the needed runtime
recovery behavior. Note that plain pkill sends TERM, not KILL.

### Task: End unresponsive managed processes without waiting for graceful systemd shutdown

**Change**

- [x] Keep the existing Linux-only, invocation-local `run --force-cli` interface
  and desktop-owned selected profile. Change only the service takeover mechanism.
- [x] Snapshot the managed units and process identities, then submit
  `systemctl --no-block stop` for existing units, with a 3-second timeout per
  systemctl invocation. Successful stop-job submission is required before sending
  signals to daemon/core processes: a bare kill can trigger `Restart=always`.
  If submission itself fails or hangs, report the specific failed command and
  abort without starting a competing core.
- [x] Read managed process identities from the verified unit cgroups, including
  descendant groups. Signal those processes directly through psutil, rather than
  waiting for `systemctl stop` to complete. Retain the current-user boundary for
  the GUI; root-owned daemon/core processes must belong to the targeted units.
  Protect against PID reuse, process exit races, and unrelated sbc processes.
- [x] Send TERM, allow up to 3 seconds, then KILL verified survivors and allow
  another 2 seconds. Do not use broad command-line matching: `pkill -f sing-box`
  can match sbc's own interpreter/launcher as well as unrelated instances.
- [x] Verify the unit groups are empty and stop jobs have completed with bounded
  final polling (5 seconds). Reuse the collected identities to detect surviving
  or restarted processes. If verification fails, print the remaining unit/PIDs and
  do not clear cache or launch the debug core. Print progress at enqueue, TERM,
  escalation, and verification so waits are visible.
- [x] Preserve foreground exit/Ctrl+C behavior and leave services stopped without
  changing persistent autostart settings. Update README, overview, and log to
  distinguish this correction from the v0.1.13 behavior.

**Verification**

1. [x] Reproduce a stop job that stays in deactivating state; verify direct TERM
   is sent without waiting for the normal systemd stop timeout, with KILL only
   for verified survivors.
2. [x] Cover a stalled stop-job submission, missing/unreadable cgroups, daemon
   and nested worker processes, reused/exited PIDs, a restarted process, and an
   unrelated CLI process. Confirm every failure prevents core launch/cache clear.
3. [x] Check ordered progress messages, short subprocess timeouts, final polling
   deadlines, current-user GUI boundaries, and unchanged profile ownership.
4. [x] Run relevant tests, static checks, repository gates, Windows mypy, and
   independent review against this correction. Do not stop host services in tests.

**Done**

- [x] A submitted but stalled stop job no longer prevents direct termination and
  foreground debugging once the managed processes have exited.
- [x] An unresponsive service manager produces a bounded, explicit failure,
  rather than a silent wait or an unsafe competing core.
- [x] The user can repeat the affected-machine check below with this correction;
  automated fake-process results are not reported as real-machine success.

### Verification evidence

- Regression suite before final test expansion: 49 passed (37 debug and 12 daemon
  tests), independently rerun by the reviewer with no actionable findings.
- Final debug tests: 39 passed, including two additional command-level failure
  cases. Both prove cache clearing and foreground launch are prevented.
- A controlled real child ignored TERM and was killed after the short grace period;
  a separate unrelated child stayed running. Neither child was a host service.
- `uv run bash scripts/pre-commit.sh`: passed; 410 tests, 87% coverage, Linux mypy,
  lint/format, and pre-commit. The two final extra test cases passed separately.
- `uv run mypy src --platform win32`: passed for 24 source files.
- All service-manager commands in tests were faked. Native `systemctl show` and
  `/proc/self/cgroup` were read only to confirm field representations, notably an
  empty `Job=` when there is no pending job.
- Reverted formatter-only changes in `tests/api/test_monitor.py` as required.
- Real daemon/TUN/cache recovery on the affected machine remains pending. This
  correction was local and uncommitted at implementation handoff. The user
  subsequently authorized commit, push, and release v0.1.14.
