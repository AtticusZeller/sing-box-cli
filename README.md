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
sbc config update --restart   # no URL: the saved subscription
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

### Where it is kept

sbc keeps its configuration the way the official sing-box desktop app does, in one
place, whether the app is installed or not:

```
<config dir>/settings.db           profiles and the selected one
<config dir>/profiles/<id>.json    the selected profile's sing-box configuration
```

The config directory is `~/.config/sing-box` on Linux and `%APPDATA%/sing-box` on
Windows. On Windows, an `ApplicationDataDirectory` registered by the official
desktop installer (layout version 2) takes precedence. The selected profile is the
configuration and its subscription URL is the subscription. A subscription token is
stored inside that URL (`?token=...`), as the app stores it. It is still sent as a
bearer token and never printed.

Without the app, sbc creates this store itself with the app's schema, so the app
takes it over unchanged if it is installed later. Files of older versions are
imported the first time sbc runs anything but `config get` or a `--dry-run`, which
create nothing:

- `config.json` becomes the profile. It stays as a link to the profile, so a
  service created before keeps running the same file. The previous file is kept as
  `config.json.migrated`.
- The URL and token of `app_config.json` become the subscription. The file is kept
  as `app_config.json.migrated`.
- A service created before follows the profile: `service restart` and
  `config update --restart` point it at the profile when it points elsewhere.

A configuration is written exactly as downloaded. The diff, and whether there is a
change at all, ignore formatting.

## sing-box desktop app

### Linux

When the official sing-box desktop app is installed, sbc drives its daemon instead
of a systemd service of its own, and the app's `settings.db` is the store. The app
counts as installed when its `sing-box-daemon.service` exists.

```bash
uv tool install 'sing-box-cli[desktop]'   # grpcio is only needed for this
sbc service restart --update              # sudo works too, it acts as the invoking user
```

| Command | With the desktop app |
|---|---|
| `service status/stop/restart/enable` | start, stop and reload the daemon's core. `restart` reloads in place, without a stop in between |
| `service disable` | stops the core. Autostart stays with the app's `sing-box-daemon.service` |
| `config update [URL]`, `--update` | refresh the profile selected in the app and record the refresh there. A new URL is saved to that profile once it downloads fine |
| `--clear-cache`, `config clear_cache` | clear the daemon's working directory, like "clear data" in the app |
| `stats/conns/proxy/logs` | unchanged, the default API address comes from the profile |
| `run` | requires `--force-cli` for explicit Linux foreground takeover |

The core is started with the start options set in the app (OOM killer, memory
limit, power report). The app's store is never created or repaired from here: if it
cannot be read, or the daemon is stopped, sbc says so instead of working around it.

- A local profile of the app has no subscription, `config update URL` is refused for it.
- The app may show an old subscription URL until its profile list is reloaded.

#### Foreground debugging when the daemon fails

If desktop service commands fail with `DEADLINE_EXCEEDED` or `UNAVAILABLE`, use:

```bash
sudo sbc run --force-cli
# Optional: refresh the selected subscription and/or clear the foreground cache
sudo sbc run --force-cli --update --clear-cache
```

This Linux-only flag bypasses desktop gRPC. It exits the invoking user's desktop
application and its workers, stops `sing-box-daemon.service` and any installed
`sing-box.service`, then runs the independent `sing-box-bin` core in the terminal.
It uses the app's selected profile and the user's sing-box configuration directory
as its working directory; it never uses the desktop executable on PATH as a core.
Failed or unverifiable stops abort the launch. Keep the desktop app closed while
debugging to avoid starting a competing core.

Takeover submits non-blocking systemd stop jobs, then directly sends TERM to
processes in the verified service cgroups, including nested workers. It allows
3 seconds for TERM and 2 seconds after KILL for survivors, then polls shutdown
and stop-job completion for up to 5 seconds. Progress messages show each stage.
Each systemctl invocation has a timeout of at most 3 seconds. If systemd cannot
accept the stop jobs, takeover aborts with the failed command instead of killing
a daemon that may be automatically restarted. Processes outside the target
service groups are not matched by name or command line.

The core prints its own diagnostics, using the profile's logging settings. This
does not enable debug-level logging or change the profile automatically. Relative
cache paths resolve under the configuration directory, so `--clear-cache` here
clears that foreground cache, rather than the daemon's private working directory.
An explicit update runs before stopping the services and aborts on failure.

After the core exits or you press Ctrl+C, the services stay stopped. No backend
preference or autostart setting is changed. Restore desktop mode with:

```bash
sudo systemctl start sing-box-daemon.service
# Then reopen the desktop application.
```

### Windows desktop app

On Windows, sbc detects the desktop app through its installed `sing-box-daemon`
service or its `HKLM\SOFTWARE\SagerNet\sing-box` installation metadata, including
when the daemon is stopped. It shares the app's selected profile and subscription:
`config get`, `config update [URL]`, and `--dry-run` work as on Linux, and
`stats/conns/proxy/logs` read the API settings from that profile. Missing or invalid
app settings are reported without creating or repairing a store.

`sbc version` reads the version from the running daemon service's executable using
its read-only `version` command. The core itself may be stopped and the Clash API
may be disabled; no daemon RPC or administrator elevation is needed.

Windows daemon control has an upstream restriction: the daemon authenticates the
installed, signed desktop application and its worker, including their parent
process relationship. Its named pipe cannot be called by a Python CLI. Therefore
`service` commands, `config update --restart` and cache clearing report this
limitation; use the desktop app to reload after `sbc config update`, or to clear
data. `run` is also refused to prevent a second core competing with the app.
The standalone Go service helper is used only when the desktop app is absent.
The `[desktop]` extra does not remove this Windows restriction.

These conventions follow the official app's
[installation layout](https://github.com/SagerNet/sing-box-for-desktop/blob/main/src/main/installationLayout.ts)
and the daemon's
[Windows authentication](https://github.com/SagerNet/sing-box/blob/testing/experimental/boxdd/peer_windows.go).

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
