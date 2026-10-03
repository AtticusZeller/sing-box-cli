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
sbc install
```

On Linux, `sbc install` installs both `sbc` and `sing-box-cli` into
`/usr/local/bin`, requesting sudo only to write those system launchers. It replaces
the old manual symlinks and can be run again safely. Use `--bin-dir PATH` to select
another existing executable directory. Unrelated files are preserved.

The system launchers use Python `-B`, so commands such as `sudo sbc service enable`
do not create root-owned `__pycache__` files in your uv environment. Run the install
command as your normal user after installing the tool. Do not run `uv tool install`,
`uv tool upgrade`, or `uv run` with sudo.

Install with specific sing-box version

```bash
uv tool install sing-box-cli --with sing-box-bin==x.y.z
```

Upgrade
```bash
uv tool upgrade sing-box-cli
```

Linux launchers continue to use the same environment after a normal uv upgrade.
Run `sbc install` again if you move or change the installation environment.

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
Architecture support follows `sing-box-bin`: Windows distributes the amd64 core
and service helper; Linux distributes amd64 and arm64 cores. No additional CPU
architectures are introduced by the service helper.

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

## Configuration

Preview an update before applying it:

```bash
sbc config update https://sub.example.com/alice.json --dry-run
sbc config update https://sub.example.com/alice.json --restart
```

`--dry-run` downloads and validates the configuration, then shows its diff without
saving configuration, subscription URL or token. It does not create local config
files or change their ownership. If combined with `--restart`, the service is left
running without a restart.

Subscription downloads retry once over IPv4 if the default connection fails or
times out. Configured environment proxies are preserved: this direct IPv4 retry
is skipped when a proxy is configured. HTTP errors such as 401/403 are not retried.

Use one command to choose what to print:

```bash
sbc config get                 # configuration JSON
sbc config get --subscription  # subscription URL
```

`config show` and `config show-sub` remain available as hidden compatibility aliases.

## Serve private GitHub configurations

Set `GH_TOKEN` (or `GITHUB_TOKEN`) with **Contents: read** access to your private
repository, then run as your normal user:

```bash
sbc config serve add https://github.com/owner/private-configs --domain sub.example.com
sbc config serve list
sbc token -c alice.json
sbc config serve start
sbc config serve stop
```

`list` prints URLs for valid root `.json` configurations. The server reads GitHub
through its API without cloning; `start` runs in the background at `127.0.0.1:8080`
by default. Use `--host` and `--port` for another listening address.

Subscription downloads require a separate token. `sbc token -c FILE.json` prints its secret
and a URL containing `?token=...` once; only its hash is stored on the server.
Use `sbc token -l` to list tokens, `sbc token -r TOKEN` to revoke one, or
`sbc token -r FILE.json` to revoke all tokens for a file. Creation infers a single
registered domain; use `-d DOMAIN` when there are multiple domains. Each token is
limited to its domain and file. The GitHub PAT stays on the server.

Point Traefik at the server using [this example](docs/traefik-config-serve.yml).
Clients continue to use the existing update command:

```bash
sbc config update https://sub.example.com/alice.json -t '<subscription-token>' --restart
# Apps without header options can use the token URL printed by token -c.
sbc config update 'https://sub.example.com/alice.json?token=<subscription-token>' --dry-run
```

See [configuration server details](docs/config-serve.md) for settings, logs and limits.
