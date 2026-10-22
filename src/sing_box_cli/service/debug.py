"""Explicit Linux desktop takeover; never depends on the desktop RPC service."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import typer
from rich import print
from sing_box_bin import get_bin_path

from ..config.config import ConfigHandler, run_args
from ..config.store import invoking_user

_APP_EXECUTABLES = (Path("/opt/sing-box/sing-box"), Path("/usr/bin/sing-box"))
_UNITS = ("sing-box-daemon.service", "sing-box.service")
_WAIT = 5
_SYSTEMCTL_TIMEOUT = 20
_CGROUP_ROOT = Path("/sys/fs/cgroup")


class DebugTakeoverError(Exception):
    """Takeover could not be verified; do not launch another core."""


def _process_uid(process: psutil.Process) -> int:
    if sys.platform != "linux":
        raise DebugTakeoverError("--force-cli is supported on Linux only.")
    return process.uids().real


def _app_processes(uid: int) -> list[psutil.Process]:
    executables = {str(path.resolve()) for path in _APP_EXECUTABLES}
    processes: dict[int, psutil.Process] = {}
    for process in psutil.process_iter():
        try:
            if _process_uid(process) != uid or process.exe() not in executables:
                continue
            for member in [process, *process.children(recursive=True)]:
                if _process_uid(member) == uid:
                    # Cache the identity before a later signal; psutil guards PID reuse.
                    member.create_time()
                    processes[member.pid] = member
        except psutil.NoSuchProcess:
            continue
    return list(processes.values())


def _stop_app(uid: int) -> None:
    processes = _app_processes(uid)
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=_WAIT)
    for process in alive:
        try:
            if _process_uid(process) != uid:
                raise DebugTakeoverError(
                    "A desktop process changed its user; aborting."
                )
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=_WAIT)
    if alive:
        raise DebugTakeoverError("Desktop processes did not exit; aborting.")


def _unit_state(unit: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=LoadState,ActiveState,MainPID,ControlPID,ControlGroup,KillMode",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=_SYSTEMCTL_TIMEOUT,
    )
    state = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    required = {"LoadState", "ActiveState", "MainPID", "ControlPID", "ControlGroup"}
    if not required.issubset(state):
        raise DebugTakeoverError(f"Cannot verify the state of {unit}.")
    return state


def _assert_stopped(unit: str, state: dict[str, str]) -> None:
    if (
        state["ActiveState"] not in ("inactive", "failed")
        or state["MainPID"] != "0"
        or state["ControlPID"] != "0"
    ):
        raise DebugTakeoverError(f"{unit} has not stopped; aborting.")
    group = state["ControlGroup"]
    if group:
        if not (_CGROUP_ROOT / "cgroup.controllers").is_file():
            raise DebugTakeoverError(f"Cannot verify the process group of {unit}.")
        try:
            # cgroup v2's populated flag includes all descendant groups.
            events = (_CGROUP_ROOT / group.lstrip("/") / "cgroup.events").read_text()
        except FileNotFoundError:
            return  # systemd removed the now-empty cgroup
        if "populated 0" not in events.splitlines():
            raise DebugTakeoverError(f"{unit} still has running processes.")


def _stop_units() -> None:
    for unit in _UNITS:
        state = _unit_state(unit)
        if state["LoadState"] == "not-found":
            if unit == "sing-box-daemon.service":
                raise DebugTakeoverError(
                    "Desktop daemon unit not found; cannot stop it."
                )
            _assert_stopped(unit, state)
            continue
        if state.get("KillMode") not in ("control-group", "mixed"):
            raise DebugTakeoverError(
                f"{unit} does not stop its process group; aborting."
            )
        subprocess.run(
            ["systemctl", "stop", unit], check=True, timeout=_SYSTEMCTL_TIMEOUT
        )
        _assert_stopped(unit, _unit_state(unit))


def _foreground(args: list[str]) -> int:
    child = subprocess.Popen(args)
    try:
        returncode = child.wait()
        return returncode if returncode >= 0 else 128 - returncode
    except KeyboardInterrupt:
        # The terminal normally signals both processes. Also handle SIGINT delivered
        # only to sbc, and reap an unresponsive child before exiting.
        child.send_signal(signal.SIGINT)
        for stop in (child.terminate, child.kill):
            try:
                child.wait(timeout=_WAIT)
                break
            except subprocess.TimeoutExpired:
                stop()
        child.wait(timeout=_WAIT)
        return 130


def run_desktop_debug(config: ConfigHandler, *, update: bool, purge: bool) -> None:
    """Validate inputs, stop competing processes and run the bundled core."""
    if sys.platform != "linux":
        print("❌ --force-cli is supported on Linux only.")
        raise typer.Exit(1)
    import pwd

    try:
        uid = pwd.getpwnam(config.user).pw_uid
        binary = Path(get_bin_path()).absolute()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise DebugTakeoverError("The bundled sing-box core is not executable.")
        # A missing profile must fail before stopping the desktop app.
        if config.config_file_content.strip() == "{}":
            raise DebugTakeoverError("Configuration file is empty.")
        subprocess.run(
            [str(binary), "version"], check=True, capture_output=True, timeout=_WAIT
        )
        if update:
            # Update while the old core can still provide network access.
            with invoking_user():
                if not config.update_config():
                    raise DebugTakeoverError("Failed to update configuration.")
        print(
            "⚠️ Stopping the desktop app and sing-box services for foreground debugging."
        )
        try:
            _stop_app(uid)
            _stop_units()
            if _app_processes(uid):
                raise DebugTakeoverError("The desktop app restarted; aborting.")
            for unit in _UNITS:
                _assert_stopped(unit, _unit_state(unit))
            if purge:
                config.clear_cache()
            print(f"🔧 Using standalone core: {binary}")
            raise typer.Exit(_foreground(run_args(config, binary=binary)))
        finally:
            print(
                "ℹ️ Services are not restarted automatically. To restore desktop mode:\n"
                "sudo systemctl start sing-box-daemon.service\n"
                "Then reopen the desktop app."
            )
    except (
        DebugTakeoverError,
        OSError,
        KeyError,
        psutil.Error,
        subprocess.SubprocessError,
    ) as e:
        print(f"❌ Foreground debug failed: {e}")
        raise typer.Exit(1) from e
