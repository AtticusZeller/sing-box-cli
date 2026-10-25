"""The desktop backend against a fake daemon speaking the real wire format."""

import json
import shutil
import sys
import tempfile
import time
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
        self.failure_method: str | None = None
        self.hang_method: str | None = None
        self.stop_status = IDLE
        self.status_reads = 0

    def service(self, details: Any) -> Any:
        method = details.method.rsplit("/", 1)[-1]
        if method == "SubscribeServiceStatus":

            def status(request: bytes, context: Any) -> Iterator[bytes]:
                self.status_reads += 1
                yield desktop._field_varint(1, self.status)

            return grpc.unary_stream_rpc_method_handler(status)

        def handle(request: bytes, context: Any) -> bytes:
            if self.rpc_failure is not None and self.failure_method in (None, method):
                context.abort(self.rpc_failure, "test daemon failure")
            if self.hang_method == method:
                while context.is_active():
                    time.sleep(0.005)
                return b""
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
            self.status = self.stop_status
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


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Systemd is recorded, never connected to the host's manager."""
    from sing_box_cli.service import debug

    originals = (debug._systemctl, debug._unit_state, debug._stop_units)
    events: list[str] = []
    state = {"LoadState": "loaded", "ActiveState": "active", "Job": ""}

    def show(unit: str, **_: Any) -> dict[str, str]:
        assert unit == "sing-box-daemon.service"
        events.append("show")
        return state.copy()

    def systemctl(args: list[str], **_: Any) -> str:
        assert args == ["--no-block", "start", "sing-box-daemon.service"]
        events.append("start daemon")
        state["ActiveState"] = "active"
        return ""

    def stop_units(units: tuple[str, ...]) -> None:
        assert units == ("sing-box-daemon.service",)
        events.append("stop daemon")
        state["ActiveState"] = "inactive"

    monkeypatch.setattr(
        desktop, "ensure_root", lambda: events.append("root"), raising=False
    )
    monkeypatch.setattr(debug, "_unit_state", show)
    monkeypatch.setattr(debug, "_systemctl", systemctl)
    monkeypatch.setattr(debug, "_stop_units", stop_units)
    return SimpleNamespace(events=events, state=state, originals=originals)


def _invoke_lifecycle(app_dir: Path, command: str) -> Any:
    manager = _manager(app_dir)
    return CliRunner().invoke(
        service_app,
        [command],
        obj=SharedContext(config=manager.config, service=manager),
    )


@pytest.mark.parametrize("running", [False, True])
def test_enable_starts_daemon_only_when_needed(
    running: bool, lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    lifecycle.state["ActiveState"] = "active" if running else "inactive"
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 0, result.output
    assert ("start daemon" in lifecycle.events) is not running
    assert lifecycle.events[0] == "root"
    assert daemon.calls == ["StartService"]
    assert "Autostart" not in result.output
    assert "Service started" in result.output


def test_disable_verifies_core_before_stopping_daemon(
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = DesktopDaemonServiceManager._service_status

    def status(manager: DesktopDaemonServiceManager) -> tuple[int, str]:
        code, message = original(manager)
        lifecycle.events.append(f"core {code}")
        return code, message

    monkeypatch.setattr(DesktopDaemonServiceManager, "_service_status", status)
    result = _invoke_lifecycle(app_dir, "disable")
    assert result.exit_code == 0, result.output
    assert daemon.calls == ["StopService"]
    assert lifecycle.events.index("core 0") < lifecycle.events.index("stop daemon")
    assert "Core stopped" in result.output
    assert "Core and desktop daemon stopped" in result.output
    assert "Autostart disabled" not in result.output


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED]
)
def test_disable_still_stops_daemon_when_rpc_fails(
    code: Any, lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    daemon.rpc_failure = code
    result = _invoke_lifecycle(app_dir, "disable")
    assert result.exit_code == 0, result.output
    assert "stop daemon" in lifecycle.events
    assert "Cannot confirm core shutdown through RPC" in result.output
    assert code.name in result.output
    assert "Core and desktop daemon stopped" in result.output


def test_disable_stop_failure_cannot_print_success(
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sing_box_cli.service import debug

    def fail(_: tuple[str, ...]) -> None:
        raise debug.DebugTakeoverError("Stop verification timed out: PIDs [42]")

    monkeypatch.setattr(debug, "_stop_units", fail)
    result = _invoke_lifecycle(app_dir, "disable")
    assert result.exit_code == 1, result.output
    assert "PIDs [42]" in result.output
    assert "Core and desktop daemon stopped" not in result.output
    assert "Autostart disabled" not in result.output


def test_restart_and_stop_do_not_manage_daemon(
    lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    for command in ("restart", "stop"):
        result = _invoke_lifecycle(app_dir, command)
        assert result.exit_code == 0, result.output
    assert lifecycle.events == []
    assert daemon.calls == ["StartService", "StopService"]


def test_disable_enable_roundtrip(
    lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    assert _invoke_lifecycle(app_dir, "disable").exit_code == 0
    assert lifecycle.state["ActiveState"] == "inactive"
    assert daemon.status == IDLE
    assert _invoke_lifecycle(app_dir, "enable").exit_code == 0
    assert lifecycle.state["ActiveState"] == "active"
    assert daemon.status == STARTED
    assert daemon.calls == ["StopService", "StartService"]
    assert lifecycle.events.index("stop daemon") < lifecycle.events.index(
        "start daemon"
    )


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_lifecycle_requires_root_before_side_effects(
    command: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied() -> None:
        print("must be run as root")
        raise typer.Exit(1)

    monkeypatch.setattr(desktop, "ensure_root", denied)
    result = _invoke_lifecycle(app_dir, command)
    assert result.exit_code == 1
    assert "root" in result.output
    assert lifecycle.events == []
    assert daemon.calls == []


def test_enable_validates_profile_before_starting_daemon(
    lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    (app_dir / "profiles/p.json").unlink()
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 1
    assert "not found" in result.output
    assert lifecycle.events == ["root"]
    assert daemon.calls == []


@pytest.mark.parametrize("command", ["enable", "disable"])
@pytest.mark.parametrize("failure", ["timeout", "denied", "missing"])
def test_system_layer_failures_are_nonzero_and_preserve_reason(
    command: str,
    failure: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from sing_box_cli.service import debug

    if failure == "missing":
        lifecycle.state["LoadState"] = "not-found"
        if command == "disable":
            monkeypatch.setattr(debug, "_stop_units", lifecycle.originals[2])
    else:
        # Exercise the real bounded wrapper, including stderr from systemctl.
        if failure == "timeout":
            error: Exception = subprocess.TimeoutExpired("systemctl", 3)
        else:
            error = subprocess.CalledProcessError(
                1, "systemctl", stderr="Access denied"
            )
        from unittest.mock import MagicMock

        for name, original in zip(
            ("_systemctl", "_unit_state", "_stop_units"),
            lifecycle.originals,
            strict=True,
        ):
            monkeypatch.setattr(debug, name, original)
        monkeypatch.setattr(debug.subprocess, "run", MagicMock(side_effect=error))
    result = _invoke_lifecycle(app_dir, command)
    assert result.exit_code == 1, result.output
    expected = {"missing": "not", "denied": "Access denied", "timeout": "3 seconds"}[
        failure
    ]
    assert expected in result.output
    assert "Service started" not in result.output
    assert "Core and desktop daemon stopped" not in result.output


@pytest.mark.parametrize("failure", ["failed", "inactive"])
def test_enable_reports_daemon_startup_failure(
    failure: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sing_box_cli.service import debug

    lifecycle.state["ActiveState"] = "inactive"

    def start(*_: Any, **__: Any) -> str:
        lifecycle.state["ActiveState"] = failure
        return ""

    monkeypatch.setattr(debug, "_systemctl", start)
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 1, result.output
    assert f"ActiveState={failure}" in result.output
    assert daemon.calls == []


@pytest.mark.parametrize("phase", ["starting", "running"])
def test_enable_readiness_has_a_deadline_and_distinguishes_running_daemon(
    phase: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "_LIFECYCLE_TIMEOUT", 0.04)
    daemon.hang_method = "GetDaemonInfo"
    if phase == "starting":
        lifecycle.state["ActiveState"] = "activating"
        lifecycle.state["Job"] = "7"
    before = time.monotonic()
    result = _invoke_lifecycle(app_dir, "enable")
    assert time.monotonic() - before < 1
    assert result.exit_code == 1, result.output
    expected = (
        "running but RPC is unresponsive"
        if phase == "running"
        else "did not become ready"
    )
    assert expected in result.output
    assert "Service started" not in result.output
    assert daemon.calls == []


@pytest.mark.parametrize("ownership", [OWNER_AVAILABLE, OWNER_OTHER])
def test_enable_preserves_ownership_error(
    ownership: int, lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    daemon.ownership = ownership
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 1, result.output
    assert (
        "no owner yet" if ownership == OWNER_AVAILABLE else "another user"
    ) in result.output
    assert "unresponsive" not in result.output
    assert daemon.calls == []


def test_enable_core_start_failure_does_not_report_success(
    lifecycle: SimpleNamespace, daemon: FakeDaemon, app_dir: Path
) -> None:
    daemon.failure_method = "StartService"
    daemon.rpc_failure = grpc.StatusCode.INVALID_ARGUMENT
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 1, result.output
    assert "INVALID_ARGUMENT" in result.output
    assert "Service started" not in result.output


@pytest.mark.parametrize("hung", ["StopService", "status"])
def test_disable_falls_back_after_bounded_core_stop_or_verification(
    hung: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "_LIFECYCLE_TIMEOUT", 0.04)
    if hung == "status":
        daemon.stop_status = STARTED  # StopService acknowledged but not stopped
    else:
        daemon.hang_method = hung
    before = time.monotonic()
    result = _invoke_lifecycle(app_dir, "disable")
    assert time.monotonic() - before < 1
    assert result.exit_code == 0, result.output
    assert "Cannot confirm core shutdown through RPC" in result.output
    assert "Core stopped (verified through RPC)" not in result.output
    assert "stop daemon" in lifecycle.events
    assert "Core and desktop daemon stopped" in result.output


def test_disable_when_daemon_is_already_inactive(
    lifecycle: SimpleNamespace, app_dir: Path
) -> None:
    lifecycle.state["ActiveState"] = "inactive"
    result = _invoke_lifecycle(app_dir, "disable")
    assert result.exit_code == 0, result.output
    assert "Cannot confirm core shutdown through RPC" in result.output
    assert "stop daemon" in lifecycle.events


def test_stopped_daemon_restart_points_to_enable_without_systemd_call(
    lifecycle: SimpleNamespace, app_dir: Path
) -> None:
    result = _invoke_lifecycle(app_dir, "restart")
    assert result.exit_code == 1
    assert "sudo sbc service enable" in result.output
    assert lifecycle.events == []


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_lifecycle_separates_user_rpc_from_privileged_system_operations(
    command: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from sing_box_cli.service import debug

    user = False

    @contextmanager
    def as_user() -> Iterator[None]:
        nonlocal user
        assert not user
        user = True
        try:
            yield
        finally:
            user = False

    original_call = DesktopDaemonServiceManager._call
    original_request = DesktopDaemonServiceManager._start_request

    def call(manager: DesktopDaemonServiceManager, *args: Any, **kwargs: Any) -> bytes:
        assert user, "RPC must run as invoking user"
        return original_call(manager, *args, **kwargs)

    def request(manager: DesktopDaemonServiceManager) -> bytes:
        assert user, "profile must be read as invoking user"
        return original_request(manager)

    original_show = debug._unit_state
    original_stop = debug._stop_units

    def show(*args: Any, **kwargs: Any) -> dict[str, str]:
        assert not user, "systemctl must run as root"
        return original_show(*args, **kwargs)

    def stop(*args: Any, **kwargs: Any) -> None:
        assert not user, "signals must run as root"
        original_stop(*args, **kwargs)

    monkeypatch.setattr(desktop, "invoking_user", as_user)
    monkeypatch.setattr(DesktopDaemonServiceManager, "_call", call)
    monkeypatch.setattr(DesktopDaemonServiceManager, "_start_request", request)
    monkeypatch.setattr(debug, "_unit_state", show)
    monkeypatch.setattr(debug, "_stop_units", stop)
    result = _invoke_lifecycle(app_dir, command)
    assert result.exit_code == 0, result.output
    assert not user


@pytest.mark.parametrize("failure", ["timeout", "denied"])
def test_enable_start_submission_failure_is_not_hidden(
    failure: str,
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    from unittest.mock import MagicMock

    from sing_box_cli.service import debug

    lifecycle.state["ActiveState"] = "inactive"
    error = (
        subprocess.TimeoutExpired("systemctl", 3)
        if failure == "timeout"
        else subprocess.CalledProcessError(1, "systemctl", stderr="Start denied")
    )
    run = MagicMock(side_effect=error)
    monkeypatch.setattr(debug, "_systemctl", lifecycle.originals[0])
    monkeypatch.setattr(debug.subprocess, "run", run)
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 1, result.output
    assert "systemctl --no-block start" in result.output
    assert ("3 seconds" if failure == "timeout" else "Start denied") in result.output
    assert run.call_args.kwargs["timeout"] == 3
    assert daemon.calls == []


def test_enable_waits_for_rpc_to_appear_then_starts_selected_profile(
    lifecycle: SimpleNamespace,
    daemon: FakeDaemon,
    app_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = desktop.desktop_daemon_available
    checks = 0

    def available() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1 and original()

    monkeypatch.setattr(desktop, "desktop_daemon_available", available)
    result = _invoke_lifecycle(app_dir, "enable")
    assert result.exit_code == 0, result.output
    assert checks >= 3  # not ready, ready, then StartService
    assert daemon.calls == ["StartService"]
    assert decode_fields(daemon.start_requests[0])[1] == b'{"log":{}}'
