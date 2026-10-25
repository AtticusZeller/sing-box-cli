# Desktop debug fallback: pending verification

Implementation and automated verification are recorded in [log.md](log.md).
The execution contract and limits are in [overview.md](overview.md).

## Validate takeover on the affected Linux machine

### Task: Confirm real desktop shutdown and foreground core execution

**Change**

- [ ] Use v0.1.14 or a later build containing the non-blocking stop correction on the
  affected machine. The user reported that v0.1.13 failed this check on 2026-10-25.
  Keep the desktop application closed after takeover. This interrupts proxy services and therefore
  remains a user-run check; no local service was stopped during implementation.

**Verification**

1. [ ] While desktop RPC is unresponsive, run `sudo sbc run --force-cli`.
2. [ ] Confirm the GUI and daemon stop and the independent core prints foreground
   startup or configuration errors without waiting for desktop gRPC.
3. [ ] Press Ctrl+C and confirm no foreground core remains. Check that services
   stay stopped and there is no competing TUN/API/cache owner.
4. [ ] Restore desktop mode with `sudo systemctl start sing-box-daemon.service`
   and reopen the app. Confirm normal desktop operation resumes.

**Done**

- [ ] Real daemon termination and TUN/cache release are observed, or any remaining
  failure is recorded with its error and process/service state.
