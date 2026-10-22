"""The desktop backend against a fake daemon speaking the real wire format."""

import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from concurrent import futures
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from sing_box_cli.config.store import SettingsError
from sing_box_cli.service import SharedContext, desktop, service as service_app
from sing_box_cli.service.desktop import (
    DesktopDaemonServiceManager,
    StartOptions,
    decode_fields,
    read_start_options,
)

grpc = pytest.importorskip("grpc")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the sing-box desktop app backend is Linux only"
)

OWNER_AVAILABLE, OWNER_CALLER, OWNER_OTHER = 1, 2, 3
IDLE, STARTED = 0, 2


class FakeDaemon(grpc.GenericRpcHandler):  # type: ignore[misc]
    """Just enough of the daemon: ownership, service status, start, stop, destroy."""

    def __init__(self, ownership: int = OWNER_CALLER) -> None:
        self.ownership = ownership
        self.status = STARTED
        self.calls: list[str] = []
        self.start_requests: list[bytes] = []
        self.rpc_failure: Any = None

    def service(self, details: Any) -> Any:
        method = details.method.rsplit("/", 1)[-1]
        if method == "SubscribeServiceStatus":
            return grpc.unary_stream_rpc_method_handler(
                lambda request, context: iter([desktop._field_varint(1, self.status)])
            )

        def handle(request: bytes, context: Any) -> bytes:
            if self.rpc_failure is not None:
                context.abort(self.rpc_failure, "test daemon failure")
            return self._unary(method, request)

        return grpc.unary_unary_rpc_method_handler(handle)

    def _unary(self, method: str, request: bytes) -> bytes:
        if method == "GetDaemonInfo":
            return desktop._field_bytes(1, b"1.14.3") + desktop._field_varint(
                2, self.ownership
            )
        self.calls.append(method)
        if method == "StartService":
            self.start_requests.append(request)
            self.status = STARTED
        elif method == "StopService":
            self.status = IDLE
        return b""


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDaemon]:
    directory = tempfile.mkdtemp(prefix="sbc-")  # unix socket paths are short
    path = Path(directory) / "d.sock"
    fake = FakeDaemon()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers((fake,))
    server.add_insecure_port(f"unix:{path}")
    server.start()
    monkeypatch.setattr(desktop, "DAEMON_SOCKET", path)
    yield fake
    server.stop(0)
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def app_dir(tmp_path: Path, make_settings_db: Callable[..., Path]) -> Path:
    """The app's data directory with profile ``p`` selected."""
    directory = tmp_path / "sing-box"
    (directory / "profiles").mkdir(parents=True)
    (directory / "profiles" / "p.json").write_text('{"log":{}}')
    make_settings_db(directory, selected="p", profiles={"p": "https://sub.example/p"})
    return directory


def _manager(app_dir: Path) -> DesktopDaemonServiceManager:
    config: Any = SimpleNamespace(config_dir=app_dir, api_base_url="")
    return DesktopDaemonServiceManager(config)


def _options_of(request: bytes) -> dict[int, int | bytes]:
    nested = decode_fields(request)[2]
    assert isinstance(nested, bytes)
    return decode_fields(nested)


def test_status_follows_the_service(daemon: FakeDaemon, app_dir: Path) -> None:
    manager = _manager(app_dir)
    assert manager.status() == "Running"

    manager.stop()
    assert manager.status() == "Stopped"
    assert daemon.calls == ["StopService"]


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE]
)
def test_rpc_failure_preserves_error_and_suggests_explicit_debug(
    code: Any, daemon: FakeDaemon, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.rpc_failure = code
    with pytest.raises(typer.Exit):
        _manager(app_dir).status()
    output = capsys.readouterr().out
    assert code.name in output
    assert "test daemon failure" in output
    assert "sudo sbc run --force-cli" in output
    assert daemon.calls == []


def test_stop_does_nothing_when_already_stopped(
    daemon: FakeDaemon, app_dir: Path
) -> None:
    daemon.status = IDLE
    _manager(app_dir).stop()
    assert daemon.calls == []


def test_start_sends_the_active_profile(daemon: FakeDaemon, app_dir: Path) -> None:
    daemon.status = IDLE
    _manager(app_dir).start()

    request = decode_fields(daemon.start_requests[0])
    assert request[1] == b'{"log":{}}'
    assert _options_of(daemon.start_requests[0]) == {2: 1, 3: 50 * 1024 * 1024}
    assert daemon.status == STARTED


def test_start_options_come_from_the_app_preferences(
    daemon: FakeDaemon, app_dir: Path, make_settings_db: Callable[..., Path]
) -> None:
    make_settings_db(
        app_dir,
        preferences={
            "oom_killer_enabled": True,
            "oom_killer_kill_connections": True,
            "oom_memory_limit_mb": 128,
            "power_report_enabled": True,
        },
    )

    assert read_start_options(app_dir) == StartOptions(
        oom_killer_enabled=True,
        oom_killer_disabled=False,
        oom_memory_limit_mb=128,
        power_report_enabled=True,
    )
    _manager(app_dir).start()
    assert _options_of(daemon.start_requests[0]) == {1: 1, 3: 128 * 1024 * 1024, 4: 1}


def test_missing_settings_stop_the_start_instead_of_guessing(
    daemon: FakeDaemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "no-app-data"
    (empty / "profiles").mkdir(parents=True)
    (empty / "profiles" / "p.json").write_text("{}")  # a guess would use it

    with pytest.raises(SettingsError):
        read_start_options(empty)
    with pytest.raises(typer.Exit):
        _manager(empty).start()

    assert "settings" in capsys.readouterr().out
    assert daemon.calls == []


def test_ownership_is_checked(
    daemon: FakeDaemon, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.ownership = OWNER_OTHER
    with pytest.raises(typer.Exit):
        _manager(app_dir).status()
    assert "owned by another user" in capsys.readouterr().out

    daemon.ownership = OWNER_AVAILABLE
    with pytest.raises(typer.Exit):
        _manager(app_dir).start()
    assert "no owner yet" in capsys.readouterr().out
    assert daemon.calls == []


def test_clear_cache_stops_destroys_and_starts_again(
    daemon: FakeDaemon, app_dir: Path
) -> None:
    _manager(app_dir).clear_cache()
    assert daemon.calls == ["StopService", "DestroyWorkingDirectory", "StartService"]

    daemon.calls.clear()
    daemon.status = IDLE
    _manager(app_dir).clear_cache()
    assert daemon.calls == ["DestroyWorkingDirectory"]  # was not running: stay stopped


def test_restart_command_reloads_in_place(daemon: FakeDaemon, app_dir: Path) -> None:
    obj = SharedContext(config=_manager(app_dir).config, service=_manager(app_dir))

    result = CliRunner().invoke(service_app, ["restart"], obj=obj)

    assert result.exit_code == 0, result.output
    assert daemon.calls == ["StartService"]  # no stop in between


def test_restart_with_clear_cache_goes_through_a_stop(
    daemon: FakeDaemon, app_dir: Path
) -> None:
    obj = SharedContext(config=_manager(app_dir).config, service=_manager(app_dir))

    result = CliRunner().invoke(service_app, ["restart", "--clear-cache"], obj=obj)

    assert result.exit_code == 0, result.output
    assert daemon.calls == ["StopService", "DestroyWorkingDirectory", "StartService"]


def test_json_of_profile_is_untouched(daemon: FakeDaemon, app_dir: Path) -> None:
    """What reaches the daemon is the profile file, byte for byte."""
    text = json.dumps({"outbounds": [], "x": "é"}, ensure_ascii=False)
    (app_dir / "profiles" / "p.json").write_text(text, encoding="utf-8")

    _manager(app_dir).start()

    assert decode_fields(daemon.start_requests[0])[1] == text.encode("utf-8")
