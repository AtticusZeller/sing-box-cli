# Linux desktop service lifecycle

`service enable` and `service disable` manage the installed desktop daemon and
its embedded core. They require root for systemd/process operations; profile
reads, ownership checks and RPC use the invoking user under sudo. The desktop
owns settings.db, profiles and autostart policy. These commands do not repair the
store, install a standalone unit, or change autostart.

`enable` validates the selected profile/start options before side effects,
checks the daemon unit, and submits a non-blocking start only if it is neither
active nor activating. It waits up to 15 seconds for active unit state and an
ownership-checked GetDaemonInfo response, then starts the selected profile using
the existing RPC. Readiness RPC attempts have at most 1 second each, capped by
the remaining deadline. The start RPC has the normal 15-second timeout. A running
but unresponsive daemon is reported separately from a failed/incomplete startup.
There is no automatic restart of an unresponsive daemon during enable.

`disable` uses one 15-second budget for ownership, status, StopService and core
status polling. It confirms Stopped before daemon shutdown in the normal path.
If RPC fails or cannot confirm Stopped, it prints that uncertainty and proceeds
to system-level shutdown. It reuses debug.py's verified stop mechanism with only
sing-box-daemon.service selected: submit a non-blocking stop, send TERM to
verified cgroup processes, wait 3 seconds, KILL surviving verified identities,
wait 2 seconds, then verify unit state, stop jobs and empty cgroups for 5 seconds.
Systemctl calls have a 3-second timeout, capped by the remaining verification
deadline. Failure to submit or verify shutdown returns nonzero; confirmed daemon
and process-group shutdown establishes full shutdown even if RPC failed.

Normal disable does not stop the GUI or sing-box.service. Debug takeover keeps its
existing broader scope. Process identities and group membership are checked
before signals; no broad process-name kill is used. The service manager must
accept the stop job so Restart=always cannot respawn the killed daemon.

`restart` remains core hot reload and never starts the daemon. `stop` remains
core-only. Missing-daemon guidance points to `sudo sbc service enable`. Windows
desktop and standalone backends retain their existing lifecycle behavior through
the default backend entry points. Linux desktop no longer reports autostart
changes it did not perform.

Tests use a real local gRPC server and simulated systemd/cgroups. They establish
wire calls, deadlines, ordering, identity boundaries and failure reporting, not
actual desktop/TUN recovery. See [plan.md](plan.md) for the affected-machine check
and [log.md](log.md) for implementation and verification evidence.
