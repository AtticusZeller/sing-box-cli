import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sing_box_cli.service import desktop, manager
from sing_box_cli.service.desktop_windows import WindowsDesktopServiceManager
from sing_box_cli.service.manager import (
    LinuxServiceManager,
    WindowsServiceManager,
    create_service,
)


def _config(**kwargs: Any) -> Any:
    defaults = {"is_windows": False, "external_config": False}
    return SimpleNamespace(**{**defaults, **kwargs})


def test_without_desktop_app_uses_systemd_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "grpc", raising=False)

    service = create_service(_config())

    assert type(service) is LinuxServiceManager
    assert service.requires_root
    assert not service.restarts_in_place
    assert "grpc" not in sys.modules


def test_desktop_app_selects_the_daemon_backend() -> None:
    service = create_service(_config(external_config=True))

    assert isinstance(service, desktop.DesktopDaemonServiceManager)


def test_windows_keeps_its_own_backend() -> None:
    service = create_service(_config(is_windows=True))

    assert type(service) is WindowsServiceManager
    assert service.requires_root
    assert not service.restarts_in_place


def test_windows_desktop_never_selects_the_standalone_service() -> None:
    service = create_service(_config(is_windows=True, external_config=True))

    assert type(service) is WindowsDesktopServiceManager
    assert not service.requires_root


def test_existing_unit_follows_the_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: calls.append(cmd))
    profile = tmp_path / "profiles" / "abc.json"
    config = _config(
        bin_path=Path("/usr/bin/sing-box"), config_file=profile, config_dir=tmp_path
    )
    service = manager.LinuxServiceManager(config)
    service.service_file = tmp_path / "sing-box.service"

    assert service.check_service() is False
    service.sync_service()
    assert not service.service_file.exists()

    service.service_file.write_text(
        service._unit_content().replace(str(profile), str(tmp_path / "config.json"))
    )
    service.sync_service()

    assert f"-c {profile} " in service.service_file.read_text()
    assert calls == [["systemctl", "daemon-reload"]]

    calls.clear()
    service.sync_service()
    assert calls == []
