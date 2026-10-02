# Sing-Box CLI

🎤 Cross-platform sing-box service manager.

![help](assets/image.png)

## Install

### uv

Windows

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### sing-box-cli

Install
```powershell
# windows
uv tool install sing-box-cli
```

```bash
# linux
uv tool install sing-box-cli
sudo ln -sf $(which sing-box-cli) /usr/local/bin/
sudo ln -sf $(which sbc) /usr/local/bin/
```

Install with specific sing-box version

```bash
uv tool install sing-box-cli --with sing-box-bin==x.y.z
```

Upgrade
```bash
uv tool upgrade sing-box-cli
```

After upgrading on Windows, run the following in an administrator PowerShell:

```powershell
sbc service enable
sbc service status
```

This refreshes the installed service paths and migrates existing NSSM installations
to the native Go service host, keeping the same `sing-box-service` name and account.
An existing running service is stopped and restarted when its executable paths change.
If that update fails, the old service target is restored and the error is reported.

The native host and core are copied into versioned directories under
`%ProgramFiles%/sing-box-cli/sing-box-service`. The running service does not depend
on uv's environment, so future uv upgrades do not overwrite its running executables.
Configuration files stay in their existing location. Windows services run at boot;
closing the terminal does not stop them. Linux continues to use the existing systemd service.

No Go installation is required when installing a published wheel. Go is needed
only to build a wheel from source; the required version is in `windows-service/go.mod`.
The wheel bundles amd64 and arm64 helpers. The selected sing-box core must also
support the host architecture (the current `sing-box-bin` Windows core is amd64).

### Logs

The service uses the user's sing-box configuration without changing its log level.
Set `log.level` to `info` or `warn` in that configuration to reduce generated logs.
`sbc logs --log-level debug` selects the logs to view; it cannot recover debug entries
that the core did not generate. For diagnostics, explicitly change the core's log level
and restart, then restore it when finished.

The native Windows host writes only warnings/errors to the Windows Application event
log (source `sing-box-service-native`). Core stdout/stderr is kept as a bounded diagnostic
tail for failures, without an additional debug log file. To persist core logs, use
sing-box's `log.output` configuration; streaming `sbc logs` continues to use the core API.
Old versioned executables are retained for rollback. After disabling the service,
its directory under Program Files may be removed manually; user configuration is separate.

## Run

Windows in Admin powershell

```powershell
sbc --help
```

Linux

```bash
sudo sbc --help
```
