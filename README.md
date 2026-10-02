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

> [!warning]
> stop service before upgrading in windows

## Run

Windows in Admin powershell

```powershell
sbc --help
```

Linux

```bash
sudo sbc --help
```

## Service logging

Background services on Windows and Linux use `warn` logging (warnings and
errors, excluding info/debug/trace). A generated `00-service-log.json` overlay
sets this level without modifying your `config.json`, including configurations
downloaded from subscriptions. The configured log output path is preserved.

After upgrading an existing installation, run these commands in an administrator
PowerShell on Windows, or with `sudo` on Linux, to update the service command:

```text
sbc service stop
sbc service enable
```

For foreground debugging, `sbc run` uses the original `config.json` and its log
level. `sbc logs --log-level debug` selects the live API log stream; it does not
raise the core's logging level. Older sing-box versions may require foreground
execution with `log.level` set to `debug` to produce debug entries.
