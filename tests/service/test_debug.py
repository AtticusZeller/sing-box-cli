"""Explicit desktop takeover without using gRPC or changing the host's services."""

import importlib
import os
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
_PROCESS_TYPE = psutil.Process


def _state(**changes: str) -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "ControlPID": "0",
        "ControlGroup": "",
        "KillMode": "control-group",
        "Job": "",
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
    process = MagicMock(spec=_PROCESS_TYPE)
    process.pid = pid
    process.uids.return_value = SimpleNamespace(real=uid)
    process.exe.return_value = executable
    process.children.return_value = []
    process.is_running.return_value = True
    process.status.return_value = psutil.STATUS_RUNNING
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


@pytest.fixture
def managed_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A stop job stays deactivating until a direct signal ends its processes."""
    root = tmp_path / "cgroups"
    root.mkdir()
    (root / "cgroup.controllers").touch()
    unit = "sing-box-daemon.service"
    group = f"/system.slice/{unit}"
    directory = root / group.lstrip("/")
    directory.mkdir(parents=True)
    (directory / "cgroup.procs").write_text("42\n")
    (directory / "cgroup.events").write_text("populated 1\n")
    proc_root = tmp_path / "proc"
    processes = {pid: _process(pid, "/app/daemon", uid=0) for pid in (42, 43, 44)}
    queued: list[str] = []

    def exit_process(pid: int) -> None:
        processes[pid].is_running.return_value = False
        for file in directory.rglob("cgroup.procs"):
            file.write_text(
                "".join(
                    f"{line}\n"
                    for line in file.read_text().splitlines()
                    if line != str(pid)
                )
            )
        populated = any(
            file.read_text().strip() for file in directory.rglob("cgroup.procs")
        )
        (directory / "cgroup.events").write_text(f"populated {int(populated)}\n")

    for pid, process in processes.items():
        (proc_root / str(pid)).mkdir(parents=True)
        (proc_root / str(pid) / "cgroup").write_text(f"0::{group}\n")
        process.terminate.side_effect = lambda pid=pid: exit_process(pid)
        process.kill.side_effect = lambda pid=pid: exit_process(pid)

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout="version")
        assert float(kwargs["timeout"]) <= 3
        if args[1] == "--no-block":
            queued.append(args[-1])
            state = _state()
        elif args[2] != unit:
            state = _state(LoadState="not-found")
        elif (directory / "cgroup.events").read_text() == "populated 1\n":
            state = _state(
                ActiveState="deactivating" if queued else "active",
                MainPID="42",
                ControlGroup=group,
                Job="7" if queued else "",
            )
        else:
            state = _state()
        return subprocess.CompletedProcess(
            args, 0, stdout="\n".join(f"{k}={v}" for k, v in state.items())
        )

    runner = MagicMock(side_effect=run)
    monkeypatch.setattr(debug, "_CGROUP_ROOT", root)
    monkeypatch.setattr(debug, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(debug, "_VERIFY_WAIT", 0.02)
    monkeypatch.setattr(debug.psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(debug.subprocess, "run", runner)
    wait = MagicMock(
        side_effect=lambda procs, **kw: (
            [p for p in procs if not p.is_running()],
            [p for p in procs if p.is_running()],
        )
    )
    monkeypatch.setattr(debug.psutil, "wait_procs", wait)
    return SimpleNamespace(
        root=root,
        directory=directory,
        group=group,
        proc_root=proc_root,
        processes=processes,
        run=runner,
        handler=run,
        queued=queued,
        wait=wait,
    )


def test_stalled_stop_job_is_finished_by_direct_term(
    managed_units: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    env = managed_units
    debug._stop_units()
    assert env.queued == ["sing-box-daemon.service"]
    env.processes[42].terminate.assert_called_once()
    env.processes[42].kill.assert_not_called()
    assert env.wait.call_args.kwargs["timeout"] == 3
    output = capsys.readouterr().out
    assert (
        output.index("non-blocking stop")
        < output.index("Sending TERM")
        < output.index("Verifying")
    )


def test_ignored_term_escalates_for_verified_survivors(
    managed_units: SimpleNamespace,
) -> None:
    env = managed_units
    env.processes[42].terminate.side_effect = None
    debug._stop_units()
    env.processes[42].terminate.assert_called_once()
    env.processes[42].kill.assert_called_once()
    assert [call.kwargs["timeout"] for call in env.wait.call_args_list] == [3, 2]


def test_nested_worker_is_included(managed_units: SimpleNamespace) -> None:
    env = managed_units
    nested = env.directory / "worker"
    nested.mkdir()
    (nested / "cgroup.procs").write_text("43\n")
    (env.proc_root / "43/cgroup").write_text(f"0::{env.group}/worker\n")
    debug._stop_units()
    env.processes[42].terminate.assert_called_once()
    env.processes[43].terminate.assert_called_once()
    env.processes[44].terminate.assert_not_called()


@pytest.mark.parametrize("failure", ["show-timeout", "stop-timeout", "stop-command"])
def test_service_manager_failure_names_command_and_never_signals(
    failure: str, managed_units: SimpleNamespace
) -> None:
    env = managed_units

    def fail(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if (failure == "show-timeout" and args[1] == "show") or (
            failure != "show-timeout" and args[1] == "--no-block"
        ):
            if failure == "stop-command":
                raise subprocess.CalledProcessError(1, args)
            raise subprocess.TimeoutExpired(args, 3)
        return env.handler(args, **kwargs)

    env.run.side_effect = fail
    with pytest.raises(debug.DebugTakeoverError, match="systemctl .*failed"):
        debug._stop_units()
    for process in env.processes.values():
        process.terminate.assert_not_called()
        process.kill.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        "missing-procs",
        "invalid-pids",
        "unsafe-group",
        "protected-pid",
        "foreign-member",
        "unreadable-procs",
    ],
)
def test_unverifiable_process_identity_prevents_stop_and_signals(
    failure: str, managed_units: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = managed_units
    if failure == "missing-procs":
        (env.directory / "cgroup.procs").unlink()
    elif failure == "invalid-pids":
        (env.directory / "cgroup.procs").write_text("invalid\n")
    elif failure == "protected-pid":
        (env.directory / "cgroup.procs").write_text(f"{os.getpid()}\n")
    elif failure == "foreign-member":
        (env.proc_root / "42/cgroup").write_text("0::/user.slice/unrelated.scope\n")
    elif failure == "unsafe-group":
        monkeypatch.setattr(debug, "_unit_state", lambda unit: _state(ControlGroup="/"))
    else:
        read = Path.read_text

        def denied(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == "cgroup.procs":
                raise PermissionError("unreadable cgroup")
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises((debug.DebugTakeoverError, PermissionError)):
        debug._stop_units()
    assert env.queued == []
    env.processes[42].terminate.assert_not_called()


def test_exited_or_reused_identity_is_not_signalled(
    managed_units: SimpleNamespace,
) -> None:
    env = managed_units
    env.processes[42].is_running.return_value = False

    # The stale cgroup snapshot still mentions this PID, but the cached identity
    # is no longer running. It must not signal whatever reused that number.
    def stop(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = env.handler(args, **kwargs)
        if args[1] == "--no-block":
            (env.directory / "cgroup.events").write_text("populated 0\n")
            (env.directory / "cgroup.procs").write_text("")
        return result

    env.run.side_effect = stop
    debug._stop_units()
    env.processes[42].terminate.assert_not_called()
    env.processes[42].kill.assert_not_called()


def test_membership_is_rechecked_before_kill(managed_units: SimpleNamespace) -> None:
    env = managed_units

    def move() -> None:
        (env.proc_root / "42/cgroup").write_text("0::/user.slice/unrelated.scope\n")

    env.processes[42].terminate.side_effect = move
    with pytest.raises(debug.DebugTakeoverError, match="PID 42 left service group"):
        debug._stop_units()
    env.processes[42].kill.assert_not_called()


def test_stop_verification_retries_a_finishing_job(
    managed_units: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = managed_units
    polls = 0

    def finishing(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        result = env.handler(args, **kwargs)
        if env.queued and args[1] == "show" and args[2] == "sing-box-daemon.service":
            polls += 1
            if polls == 1:
                result.stdout = result.stdout.replace("Job=", "Job=9")
        return result

    env.run.side_effect = finishing
    monkeypatch.setattr(debug.time, "sleep", lambda _: None)
    debug._stop_units()
    assert polls == 2


@pytest.mark.parametrize("failure", ["survivor", "new-worker", "pending-job"])
def test_final_verification_rejects_survivors_or_pending_jobs(
    failure: str, managed_units: SimpleNamespace
) -> None:
    env = managed_units
    if failure == "survivor":
        env.processes[42].terminate.side_effect = None
        env.processes[42].kill.side_effect = None
    elif failure == "new-worker":

        def spawn() -> None:
            env.processes[42].is_running.return_value = False
            (env.directory / "cgroup.procs").write_text("44\n")

        env.processes[42].terminate.side_effect = spawn
    else:

        def pending(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = env.handler(args, **kwargs)
            if args[1] == "show" and args[2] == "sing-box-daemon.service":
                result.stdout = result.stdout.replace("Job=", "Job=9")
            return result

        env.run.side_effect = pending
    with pytest.raises(debug.DebugTakeoverError, match="verification timed out"):
        debug._stop_units()


@pytest.mark.parametrize("failure", ["survivor", "submit-timeout", "missing-procs"])
def test_stop_failure_cannot_clear_cache_or_launch_core(
    failure: str,
    config: MagicMock,
    managed_units: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = managed_units
    if failure == "survivor":
        env.processes[42].terminate.side_effect = None
        env.processes[42].kill.side_effect = None
    elif failure == "missing-procs":
        (env.directory / "cgroup.procs").unlink()
    else:

        def timeout(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if args[1] == "--no-block":
                raise subprocess.TimeoutExpired(args, 3)
            return env.handler(args, **kwargs)

        env.run.side_effect = timeout
    monkeypatch.setattr(debug, "_stop_app", lambda _: None)
    launch = MagicMock()
    monkeypatch.setattr(debug, "_foreground", launch)
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["run", "--force-cli", "--clear-cache"])
    assert result.exit_code == 1
    assert "Foreground debug failed" in result.output
    config.clear_cache.assert_not_called()
    launch.assert_not_called()


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


def test_real_unresponsive_child_is_killed_without_signalling_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "sing-box-daemon.service"
    group = f"/system.slice/{unit}"
    root = tmp_path / "cgroups"
    directory = root / group.lstrip("/")
    directory.mkdir(parents=True)
    (root / "cgroup.controllers").touch()
    script = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n"
        (directory / "cgroup.procs").write_text(f"{child.pid}\n")
        (directory / "cgroup.events").write_text("populated 1\n")
        proc_root = tmp_path / "proc"
        (proc_root / str(child.pid)).mkdir(parents=True)
        (proc_root / str(child.pid) / "cgroup").write_text(f"0::{group}\n")
        queued = []

        def state(_unit: str, **kwargs: object) -> dict[str, str]:
            if child.poll() is None:
                return _state(
                    ActiveState="deactivating" if queued else "active",
                    MainPID=str(child.pid),
                    ControlGroup=group,
                    Job="7" if queued else "",
                )
            (directory / "cgroup.procs").write_text("")
            (directory / "cgroup.events").write_text("populated 0\n")
            return _state()

        monkeypatch.setattr(debug, "_CGROUP_ROOT", root)
        monkeypatch.setattr(debug, "_PROC_ROOT", proc_root)
        monkeypatch.setattr(debug, "_UNITS", (unit,))
        monkeypatch.setattr(debug, "_TERM_WAIT", 0.05)
        monkeypatch.setattr(debug, "_unit_state", state)
        monkeypatch.setattr(debug, "_systemctl", lambda args: queued.append(args) or "")
        debug._stop_units()
        assert queued == [["--no-block", "stop", unit]]
        assert child.poll() is not None
        assert unrelated.poll() is None
    finally:
        for process in (child, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()


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
