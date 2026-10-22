"""Explicit desktop takeover without using gRPC or changing the host's services."""

import importlib
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest
import typer
from typer.testing import CliRunner

from sing_box_cli.config.config import ConfigHandler
from sing_box_cli.service import debug

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux takeover only")


def _state(**changes: str) -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "ControlPID": "0",
        "ControlGroup": "",
        "KillMode": "control-group",
        **changes,
    }


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    cfg = MagicMock(spec=ConfigHandler)
    cfg.external_config = True
    cfg.is_windows = False
    cfg.user = "test-user"
    cfg.config_dir = tmp_path
    cfg.config_file = tmp_path / "profiles/p.json"
    cfg.config_file_content = '{"log": {}}'
    # Deliberately wrong: fallback must not execute the desktop app on PATH.
    cfg.bin_path = Path("/opt/sing-box/sing-box")
    binary = tmp_path / "core"
    binary.write_text("fake core")
    binary.chmod(0o755)
    monkeypatch.setattr(debug, "get_bin_path", lambda: binary)
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000))
    main = importlib.import_module("sing_box_cli.main")
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    service = MagicMock()
    service.status.side_effect = AssertionError("must not call gRPC")
    service.stop.side_effect = AssertionError("must not call gRPC")
    monkeypatch.setattr(main, "create_service", lambda _: service)
    monkeypatch.setattr(main, "ensure_root", lambda: None)
    return cfg


@pytest.fixture
def takeover(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(
        debug.subprocess, "run", lambda *a, **kw: events.append("version")
    )
    monkeypatch.setattr(debug, "_stop_app", lambda _: events.append("app"))
    monkeypatch.setattr(debug, "_stop_units", lambda: events.append("units"))
    monkeypatch.setattr(debug, "_app_processes", lambda _: [])
    monkeypatch.setattr(debug, "_unit_state", lambda _: _state())
    monkeypatch.setattr(debug, "_foreground", lambda _: events.append("core") or 0)
    return events


def test_explicit_flag_is_required(config: MagicMock, takeover: list[str]) -> None:
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["run"])
    assert result.exit_code == 1
    assert "sudo sbc run --force-cli" in result.output
    assert takeover == []


def test_takeover_uses_core_and_profile_without_rpc(
    config: MagicMock, takeover: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    launch = MagicMock(return_value=23)
    monkeypatch.setattr(debug, "_foreground", launch)
    result = CliRunner().invoke(main.app, ["run", "--force-cli"])
    assert result.exit_code == 23, result.output
    assert takeover == ["version", "app", "units"]
    launch.assert_called_once_with(
        [
            str(config.config_dir / "core"),
            "run",
            "-c",
            str(config.config_file),
            "-D",
            str(config.config_dir),
        ]
    )
    assert "sudo systemctl start sing-box-daemon.service" in result.output
    assert config.external_config is True
    config.init_directories.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["privileges", "profile", "binary", "update", "platform"]
)
def test_preflight_failure_keeps_services_running(
    failure: str,
    config: MagicMock,
    takeover: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    flags = ["run", "--force-cli"]
    if failure == "privileges":
        monkeypatch.setattr(main, "ensure_root", MagicMock(side_effect=typer.Exit(1)))
    elif failure == "profile":
        config.config_file_content = "{}"
    elif failure == "binary":
        (config.config_dir / "core").unlink()
    elif failure == "update":
        flags.append("--update")
        config.update_config.return_value = False
    else:
        monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(main.app, flags)
    assert result.exit_code == 1, result.output
    assert "app" not in takeover
    assert "units" not in takeover
    assert "core" not in takeover


def test_update_before_takeover_and_cache_after_stop(
    config: MagicMock, takeover: list[str]
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    config.update_config.side_effect = lambda: takeover.append("update") or True
    config.clear_cache.side_effect = lambda: takeover.append("cache")
    result = CliRunner().invoke(
        main.app, ["run", "--force-cli", "--update", "--clear-cache"]
    )
    assert result.exit_code == 0, result.output
    assert takeover == ["version", "update", "app", "units", "cache", "core"]


@pytest.mark.parametrize("phase", ["app", "units", "restarted"])
def test_failed_takeover_never_launches_core(
    phase: str, config: MagicMock, takeover: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    if phase == "restarted":
        monkeypatch.setattr(debug, "_app_processes", lambda _: [MagicMock()])
    else:

        def fail(*args: object) -> None:
            raise debug.DebugTakeoverError("stop failed")

        monkeypatch.setattr(
            debug, "_stop_app" if phase == "app" else "_stop_units", fail
        )
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["run", "--force-cli", "--clear-cache"])
    assert result.exit_code == 1
    assert "core" not in takeover
    config.clear_cache.assert_not_called()
    assert "not restarted automatically" in result.output


def _process(pid: int, executable: str, uid: int = 1000) -> MagicMock:
    process = MagicMock(spec=psutil.Process)
    process.pid = pid
    process.uids.return_value = SimpleNamespace(real=uid)
    process.exe.return_value = executable
    process.children.return_value = []
    return process


def test_process_selection_is_scoped_to_app_and_invoking_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _process(11, "/app/sing-box")
    worker = _process(12, "/app/worker")
    foreign_worker = _process(13, "/app/worker", uid=1001)
    app.children.return_value = [worker, foreign_worker]
    foreign_app = _process(14, "/app/sing-box", uid=1001)
    unrelated = _process(15, "/usr/bin/unrelated")
    monkeypatch.setattr(debug, "_APP_EXECUTABLES", (Path("/app/sing-box"),))
    monkeypatch.setattr(
        debug.psutil, "process_iter", lambda: iter([app, foreign_app, unrelated])
    )
    assert debug._app_processes(1000) == [app, worker]


def test_termination_escalates_only_for_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _process(11, "/app/sing-box")
    worker = _process(12, "/app/worker")
    monkeypatch.setattr(debug, "_app_processes", lambda _: [app, worker])
    wait = MagicMock(side_effect=[([worker], [app]), ([app], [])])
    monkeypatch.setattr(debug.psutil, "wait_procs", wait)
    debug._stop_app(1000)
    app.terminate.assert_called_once()
    worker.terminate.assert_called_once()
    app.kill.assert_called_once()
    worker.kill.assert_not_called()
    assert all(call.kwargs["timeout"] == debug._WAIT for call in wait.call_args_list)


@pytest.mark.parametrize(
    "failure", ["timeout", "command", "active", "worker", "missing", "killmode"]
)
def test_systemd_stop_failure_is_reported(
    failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _state(ActiveState="active", MainPID="42")
    after = _state()
    run = MagicMock()
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("systemctl", 20)
    elif failure == "command":
        run.side_effect = subprocess.CalledProcessError(1, "systemctl")
    elif failure == "active":
        after["ActiveState"] = "active"
    elif failure == "worker":
        after["ControlGroup"] = "/system.slice/sing-box-daemon.service"
        (tmp_path / "cgroup.controllers").touch()
        group = tmp_path / after["ControlGroup"].lstrip("/")
        group.mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        monkeypatch.setattr(debug, "_CGROUP_ROOT", tmp_path)
    elif failure == "missing":
        before = _state(LoadState="not-found")
    else:
        before["KillMode"] = "process"
    monkeypatch.setattr(debug, "_unit_state", MagicMock(side_effect=[before, after]))
    monkeypatch.setattr(debug.subprocess, "run", run)
    with pytest.raises((debug.DebugTakeoverError, subprocess.SubprocessError)):
        debug._stop_units()


def test_stop_both_units_and_accept_absent_standalone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock()
    monkeypatch.setattr(debug.subprocess, "run", run)
    monkeypatch.setattr(
        debug,
        "_unit_state",
        MagicMock(
            side_effect=[
                _state(ActiveState="active"),
                _state(),
                _state(LoadState="not-found"),
            ]
        ),
    )
    debug._stop_units()
    run.assert_called_once_with(
        ["systemctl", "stop", "sing-box-daemon.service"], check=True, timeout=20
    )


def test_unreadable_systemd_state_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        debug.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="LoadState=loaded\n"),
    )
    with pytest.raises(debug.DebugTakeoverError, match="Cannot verify"):
        debug._unit_state("sing-box-daemon.service")


def test_foreground_forwards_real_exit_status() -> None:
    assert debug._foreground([sys.executable, "-c", "raise SystemExit(17)"]) == 17


def test_ctrl_c_reaps_unresponsive_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child = MagicMock()
    child.wait.side_effect = [
        KeyboardInterrupt,
        subprocess.TimeoutExpired("core", 5),
        subprocess.TimeoutExpired("core", 5),
        -9,
    ]
    monkeypatch.setattr(debug.subprocess, "Popen", lambda _: child)
    assert debug._foreground(["core"]) == 130
    child.send_signal.assert_called_once_with(signal.SIGINT)
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    assert child.wait.call_count == 4


def test_real_store_is_preserved(
    tmp_path: Path,
    make_settings_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    takeover: list[str],
) -> None:
    from sing_box_cli.service import desktop

    directory = tmp_path / ".config/sing-box"
    (directory / "profiles").mkdir(parents=True)
    profile = directory / "profiles/p.json"
    profile.write_text('{"log":{}}')
    db = make_settings_db(directory, selected="p", profiles={"p": None})
    before = {p: p.read_bytes() for p in (db, profile)}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "")
    monkeypatch.setenv("SUDO_USER", "")
    monkeypatch.setattr(ConfigHandler, "user", property(lambda _: ""))
    # ConfigHandler uses ~<user>; empty user resolves the test HOME.
    monkeypatch.setattr(desktop, "desktop_app_installed", lambda: True)
    cfg = ConfigHandler()
    assert cfg.init_directories()
    assert cfg.config_file == profile
    assert cfg.external_config
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000))
    monkeypatch.setattr(debug, "get_bin_path", lambda: sys.executable)
    main = importlib.import_module("sing_box_cli.main")
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    monkeypatch.setattr(main, "ensure_root", lambda: None)
    result = CliRunner().invoke(main.app, ["run", "--force-cli"])
    assert result.exit_code == 0, result.output
    assert "core" in takeover
    assert {p: p.read_bytes() for p in before} == before
    assert not (directory / "config.json").exists()
