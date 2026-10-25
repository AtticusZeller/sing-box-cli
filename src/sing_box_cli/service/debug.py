"""Explicit Linux desktop takeover; never depends on the desktop RPC service."""

import os
import signal
import subprocess
import sys
import time
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
_SYSTEMCTL_TIMEOUT = 3
_TERM_WAIT = 3
_KILL_WAIT = 2
_VERIFY_WAIT = 5
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_ROOT = Path("/proc")


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


def _systemctl(args: list[str], *, timeout: float = _SYSTEMCTL_TIMEOUT) -> str:
    command = ["systemctl", *args]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as e:
        detail = getattr(e, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        reason = f"; {detail.strip()}" if detail else ""
        raise DebugTakeoverError(f"{' '.join(command)} failed: {e}{reason}") from e
    return result.stdout


def _unit_state(unit: str, *, timeout: float = _SYSTEMCTL_TIMEOUT) -> dict[str, str]:
    output = _systemctl(
        [
            "show",
            unit,
            "--property=LoadState,ActiveState,MainPID,ControlPID,ControlGroup,KillMode,Job",
        ],
        timeout=timeout,
    )
    state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    required = {
        "LoadState",
        "ActiveState",
        "MainPID",
        "ControlPID",
        "ControlGroup",
        "Job",
    }
    if not required.issubset(state):
        raise DebugTakeoverError(f"Cannot verify the state of {unit}.")
    return state


def _assert_stopped(unit: str, state: dict[str, str]) -> None:
    if (
        state["ActiveState"] not in ("inactive", "failed")
        or state["MainPID"] != "0"
        or state["ControlPID"] != "0"
        or state["Job"] not in ("", "0")
    ):
        group = state["ControlGroup"]
        pids = sorted(_group_pids(_group_directory(unit, group))) if group else []
        raise DebugTakeoverError(
            f"{unit} has not stopped: MainPID={state['MainPID']}, "
            f"ControlPID={state['ControlPID']}, Job={state['Job']}, PIDs={pids}; aborting."
        )
    group = state["ControlGroup"]
    if group:
        _assert_group_empty(unit, group)


def _assert_group_empty(unit: str, group: str) -> None:
    directory = _group_directory(unit, group)
    try:
        # cgroup v2's populated flag includes all descendant groups.
        events = (directory / "cgroup.events").read_text()
    except FileNotFoundError:
        if directory.exists():
            raise DebugTakeoverError(f"Cannot read cgroup events for {unit}.")
        return  # systemd removed the now-empty cgroup
    if "populated 0" not in events.splitlines():
        raise DebugTakeoverError(
            f"{unit} still has running processes: PIDs {sorted(_group_pids(directory))}"
        )


def _group_directory(unit: str, group: str) -> Path:
    path = Path(group)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name != unit
        or not (_CGROUP_ROOT / "cgroup.controllers").is_file()
    ):
        raise DebugTakeoverError(f"Cannot verify the process group of {unit}: {group}")
    directory = _CGROUP_ROOT / group.lstrip("/")
    if not directory.resolve().is_relative_to(_CGROUP_ROOT.resolve()):
        raise DebugTakeoverError(f"Process group escapes the cgroup root: {group}")
    return directory


def _group_pids(directory: Path) -> set[int]:
    """Read nested cgroups without swallowing permission or malformed-data errors."""
    try:
        content = (directory / "cgroup.procs").read_text()
        children = list(directory.iterdir())
    except FileNotFoundError:
        if directory.exists():
            raise DebugTakeoverError(f"Cannot read processes in {directory}")
        return set()  # the whole group was removed after its last process exited
    try:
        pids = {int(line) for line in content.splitlines()}
    except ValueError as e:
        raise DebugTakeoverError(f"Invalid process list in {directory}") from e
    if any(pid <= 1 or pid in (os.getpid(), os.getppid()) for pid in pids):
        raise DebugTakeoverError(f"Refusing to signal protected PIDs in {directory}")
    for child in children:
        if child.is_dir():
            pids.update(_group_pids(child))
    return pids


def _belongs_to_group(process: psutil.Process, group: str) -> bool:
    if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
        return False
    try:
        lines = (_PROC_ROOT / str(process.pid) / "cgroup").read_text().splitlines()
    except FileNotFoundError:
        return False
    if not any(
        line == f"0::{group}" or line.startswith(f"0::{group}/") for line in lines
    ):
        raise DebugTakeoverError(
            f"PID {process.pid} left service group {group}; aborting."
        )
    return True


def _managed_processes(groups: dict[str, str]) -> dict[int, tuple[psutil.Process, str]]:
    processes: dict[int, tuple[psutil.Process, str]] = {}
    for unit, group in groups.items():
        for pid in _group_pids(_group_directory(unit, group)):
            try:
                process = psutil.Process(pid)
                process.create_time()  # psutil signals check this identity for PID reuse
                if _belongs_to_group(process, group):
                    processes[pid] = process, group
            except psutil.NoSuchProcess:
                pass
    return processes


def _signal_managed(
    processes: dict[int, tuple[psutil.Process, str]], *, kill: bool = False
) -> list[psutil.Process]:
    signalled = []
    for process, group in processes.values():
        try:
            if _belongs_to_group(process, group):
                if kill:
                    process.kill()
                else:
                    process.terminate()
                signalled.append(process)
        except psutil.NoSuchProcess:
            pass
    return signalled


def _verify_stopped(
    states: dict[str, dict[str, str]], processes: dict[int, tuple[psutil.Process, str]]
) -> None:
    deadline = time.monotonic() + _VERIFY_WAIT
    last_error = ""
    while time.monotonic() < deadline:
        try:
            for unit, original in states.items():
                timeout = min(
                    _SYSTEMCTL_TIMEOUT, max(0.001, deadline - time.monotonic())
                )
                _assert_stopped(unit, _unit_state(unit, timeout=timeout))
                # Also check the original group if systemd has cleared ControlGroup.
                if original["ControlGroup"]:
                    _assert_group_empty(unit, original["ControlGroup"])
            remaining = [
                p.pid
                for p, _ in processes.values()
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            ]
            if remaining:
                raise DebugTakeoverError(f"Service processes remain: PIDs {remaining}")
            return
        except (DebugTakeoverError, psutil.NoSuchProcess) as e:
            last_error = str(e)
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise DebugTakeoverError(f"Stop verification timed out: {last_error}")


def _stop_units(units: tuple[str, ...] | None = None) -> None:
    """Stop and verify the requested units, independently of desktop RPC."""
    states = {unit: _unit_state(unit) for unit in (_UNITS if units is None else units)}
    groups: dict[str, str] = {}
    loaded = []
    for unit, state in states.items():
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
        if state["ControlGroup"]:
            groups[unit] = state["ControlGroup"]
        elif state["MainPID"] != "0" or state["ControlPID"] != "0":
            raise DebugTakeoverError(f"Cannot locate running processes of {unit}.")
        loaded.append(unit)
    processes = _managed_processes(groups)
    for unit in loaded:
        print(f"⏳ Submitting non-blocking stop: {unit}")
        _systemctl(["--no-block", "stop", unit])
    # Catch children created between the initial snapshot and stop-job submission.
    processes.update(_managed_processes(groups))
    print(f"⏳ Sending TERM to managed PIDs: {sorted(processes)}")
    signalled = _signal_managed(processes)
    _, alive = psutil.wait_procs(signalled, timeout=_TERM_WAIT)
    survivors = {p.pid: processes[p.pid] for p in alive}
    if survivors:
        print(f"⚠️ Sending KILL to surviving managed PIDs: {sorted(survivors)}")
        signalled = _signal_managed(survivors, kill=True)
        psutil.wait_procs(signalled, timeout=_KILL_WAIT)
    print("⏳ Verifying service processes and stop jobs have finished.")
    _verify_stopped(states, processes)


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
