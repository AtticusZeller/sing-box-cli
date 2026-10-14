"""The sing-box desktop app backend: detection, start options and the wire format."""

import socket
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from sing_box_cli.config.store import SettingsError
from sing_box_cli.service import desktop
from sing_box_cli.service.desktop import (
    DesktopDaemonServiceManager,
    StartOptions,
    decode_fields,
    encode_start_request,
    read_start_options,
)

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="the sing-box desktop app backend is Linux only"
)


@linux_only
def test_desktop_app_is_detected_by_its_socket_or_its_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not desktop.desktop_app_installed()

    # a regular file where the socket would be is not a daemon
    fake = tmp_path / "sing-box.socket"
    fake.write_text("")
    monkeypatch.setattr(desktop, "DAEMON_SOCKET", fake)
    assert not desktop.desktop_app_installed()

    sock_path = tmp_path / "d.sock"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(sock_path))
        monkeypatch.setattr(desktop, "DAEMON_SOCKET", sock_path)
        assert desktop.desktop_app_installed()

    # installed but stopped: the unit is there, the socket is not
    monkeypatch.setattr(desktop, "DAEMON_SOCKET", tmp_path / "gone.socket")
    unit = tmp_path / "sing-box-daemon.service"
    unit.write_text("")
    monkeypatch.setattr(desktop, "DAEMON_UNIT", unit)
    assert desktop.desktop_app_installed()
    assert not desktop.desktop_daemon_available()


def test_backend_needs_no_root_and_reloads_in_place() -> None:
    config: Any = SimpleNamespace()
    service = DesktopDaemonServiceManager(config)

    assert not service.requires_root
    assert service.hot_reload
    assert service.check_service()


def test_stopped_daemon_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    """App installed, daemon down: say so instead of failing somewhere deeper."""
    config: Any = SimpleNamespace()
    service = DesktopDaemonServiceManager(config)

    with pytest.raises(typer.Exit):
        service.status()
    assert "systemctl start sing-box-daemon.service" in capsys.readouterr().out


def test_start_options_have_the_apps_defaults_when_never_set(
    tmp_path: Path, make_settings_db: Callable[..., Path]
) -> None:
    make_settings_db(tmp_path)

    assert read_start_options(tmp_path) == StartOptions()
    assert (
        encode_start_request("{}") == b"\x0a\x02{}\x12\x07\x10\x01\x18\x80\x80\x80\x19"
    )


def test_start_options_come_from_the_app_preferences(
    tmp_path: Path, make_settings_db: Callable[..., Path]
) -> None:
    make_settings_db(
        tmp_path,
        preferences={
            "oom_killer_enabled": True,
            "oom_killer_kill_connections": True,
            "oom_memory_limit_mb": 128,
            "power_report_enabled": True,
        },
    )

    options = read_start_options(tmp_path)

    assert options == StartOptions(
        oom_killer_enabled=True,
        oom_killer_disabled=False,
        oom_memory_limit_mb=128,
        power_report_enabled=True,
    )
    request = decode_fields(encode_start_request("{}", options))
    assert request[1] == b"{}"
    assert isinstance(request[2], bytes)
    assert decode_fields(request[2]) == {1: 1, 3: 128 * 1024 * 1024, 4: 1}


def test_start_options_are_not_guessed_without_the_settings(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        read_start_options(tmp_path)


def test_protobuf_wire_roundtrip() -> None:
    # ServiceStatus{status=STARTED(2), errorMessage="x"}
    assert decode_fields(b"\x08\x02\x12\x01x") == {1: 2, 2: b"x"}
    # DaemonInfo{version="1.14.3", ownership=CALLER(2)}
    info = decode_fields(b"\x0a\x061.14.3\x10\x02")
    assert info == {1: b"1.14.3", 2: 2}
    # fixed-width fields are skipped, anything else is refused
    assert decode_fields(b"\x09" + bytes(8) + b"\x15" + bytes(4) + b"\x08\x01") == {
        1: 1
    }
    with pytest.raises(ValueError):
        decode_fields(b"\x0b")
