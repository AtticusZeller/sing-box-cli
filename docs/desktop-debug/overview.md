# Desktop foreground debug fallback

Linux `sbc run --force-cli` is an explicit, invocation-local takeover. Normal
desktop service commands still use the desktop backend. Windows takeover is
unsupported and is rejected before stopping processes or services.

The fallback keeps desktop configuration ownership. It reads the selected profile
without repairing settings.db, validates the bundled core, optionally updates the
subscription while networking is still available, then terminates the invoking
user's desktop process tree and stops both managed services through systemd.
Only verified application executable identities and their workers are targeted.
Failure to verify service shutdown prevents core launch; nonempty cgroups require
cgroup v2 verification. The foreground core uses the profile and configuration
directory rather than the desktop daemon's private working directory.

The service-stop correction uses `systemctl --no-block stop` with a 3-second
per-call timeout, then directly signals the snapshotted service cgroup processes.
Nested workers are included, and `/proc/<pid>/cgroup` membership plus psutil process
identity is checked before each signal. TERM gets 3 seconds; verified survivors
receive KILL with another 2 seconds. A final 5-second polling phase checks the
original and current groups, tracked identities, and pending systemd jobs.
If the service manager cannot accept a stop job, takeover fails before signalling
managed processes so that `Restart=always` cannot create a competing core.
This replaces v0.1.13's synchronous `systemctl stop` path, which timed out without
direct daemon termination. The user's report established a runtime gap; earlier
passing mocks did not verify that case. Runtime validation remains pending.

Exit status is forwarded. Ctrl+C signals and reaps the child, escalating to TERM
and KILL with bounded waits if needed. Services remain stopped; restoring desktop
mode requires starting its daemon and reopening the app. Autostart and backend
preferences are unchanged. Reopening the app during debugging can start a competing
core and must be avoided.

See the [README](../../README.md#foreground-debugging-when-the-daemon-fails) for
commands and cache semantics, [log.md](log.md) for implementation evidence, and
[plan.md](plan.md) for the pending real-machine verification.
