"""A service that did not come up must not be reported as started."""

import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from sing_box_cli.config.config import ConfigHandler
from sing_box_cli.service import manager as manager_module
from sing_box_cli.service.manager import LinuxServiceManager


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LinuxServiceManager:
    config = MagicMock(spec=ConfigHandler)
    config.bin_path = Path("/usr/bin/sing-box")
    config.config_dir = tmp_path
    config.config_file = tmp_path / "config.json"
    config.api_base_url = ""
    monkeypatch.setattr(manager_module.time, "sleep", lambda _seconds: None)
    service = LinuxServiceManager(config)
    service.service_file = tmp_path / "sing-box.service"
    return service


def mock_systemctl(
    monkeypatch: pytest.MonkeyPatch, **exit_codes: int
) -> list[list[str]]:
    """Replace systemctl: an action exits with the code given for it, 0 otherwise."""
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        action = command[1].replace("-", "_")
        return subprocess.CompletedProcess(command, exit_codes.get(action, 0))

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("action", ["start", "restart"])
def test_service_that_stays_up_is_started(
    manager: LinuxServiceManager, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    calls = mock_systemctl(monkeypatch)

    getattr(manager, action)()

    assert calls == [
        ["systemctl", action, "sing-box"],
        ["systemctl", "is-active", "--quiet", "sing-box"],
    ]


@pytest.mark.parametrize("action", ["start", "restart"])
def test_refused_start_ends_the_command(
    manager: LinuxServiceManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    """For example the unit's start limit, hit after a few failed starts."""
    calls = mock_systemctl(monkeypatch, **{action: 1})

    with pytest.raises(typer.Exit) as failure:
        getattr(manager, action)()

    assert failure.value.exit_code == 1
    assert calls == [["systemctl", action, "sing-box"]]
    assert f"Failed to {action} service" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["start", "restart"])
def test_core_that_exits_right_after_starting_ends_the_command(
    manager: LinuxServiceManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    """systemd is done once the core is executed, a rejected configuration is not seen."""
    mock_systemctl(monkeypatch, is_active=3)

    with pytest.raises(typer.Exit) as failure:
        getattr(manager, action)()

    assert failure.value.exit_code == 1
    assert "journalctl -u sing-box" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "success"),
    [
        (["service", "enable"], "Service started"),
        (["service", "restart"], "Service restarted"),
    ],
)
def test_commands_do_not_report_success_on_start_failure(
    manager: LinuxServiceManager,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    success: str,
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    service = importlib.import_module("sing_box_cli.service")
    monkeypatch.setattr(main, "get_config", lambda: manager.config)
    monkeypatch.setattr(main, "create_service", lambda _config: manager)
    monkeypatch.setattr(service, "ensure_root", lambda: None)

    mock_systemctl(monkeypatch)
    result = CliRunner().invoke(main.app, command)
    assert result.exit_code == 0, result.output
    assert success in result.output

    mock_systemctl(monkeypatch, start=1)
    result = CliRunner().invoke(main.app, command)
    assert result.exit_code == 1
    assert success not in result.output
