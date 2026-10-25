# Desktop lifecycle: pending real-machine verification

Implementation and automated evidence for [issue #44](https://github.com/AtticusZeller/sing-box-cli/issues/44)
are preserved in [log.md](log.md). The command contract is in [overview.md](overview.md).
Local source contains the fix; no release has been made for this issue.

## Validate complete lifecycle on the affected Linux desktop

### Task: Observe daemon/core shutdown and recovery

**Change**

- [ ] Use a build containing the #44 lifecycle changes and the existing desktop
  settings/profile. This check interrupts proxy networking and is user-run.

**Verification**

1. [ ] With daemon inactive, run `sudo sbc service enable`: observe daemon startup,
   RPC readiness and selected-profile core start; confirm proxy traffic works.
2. [ ] Repeat enable while active: no duplicate daemon, selected core still works.
3. [ ] Run `sudo sbc service disable`: observe core Stopped verification before
   daemon shutdown, then `systemctl show sing-box-daemon.service` reports
   ActiveState=inactive/failed, MainPID=0, ControlPID=0 and no pending Job. Confirm
   its cgroup has no remaining processes and its TUN resources are released.
4. [ ] Run enable again and confirm the selected profile and proxy recover.
5. [ ] With RPC unresponsive, run disable: observe the explicit unconfirmed-core
   warning, system-level shutdown and verified empty daemon process group; then
   enable and confirm recovery. Capture output and exit codes if any step fails.
6. [ ] Check `service stop` retains daemon and `service restart` does not start an
   inactive daemon; the latter points to `sudo sbc service enable`.

**Done**

- [ ] Actual daemon/core termination, TUN release and disable/enable recovery are
  observed, or remaining failures are recorded with service/process state.
