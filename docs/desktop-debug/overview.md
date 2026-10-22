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

Exit status is forwarded. Ctrl+C signals and reaps the child, escalating to TERM
and KILL with bounded waits if needed. Services remain stopped; restoring desktop
mode requires starting its daemon and reopening the app. Autostart and backend
preferences are unchanged. Reopening the app during debugging can start a competing
core and must be avoided.

See the [README](../../README.md#foreground-debugging-when-the-daemon-fails) for
commands and cache semantics, [log.md](log.md) for implementation evidence, and
[plan.md](plan.md) for the pending real-machine verification.
