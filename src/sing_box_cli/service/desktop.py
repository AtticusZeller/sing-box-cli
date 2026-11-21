import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import typer

from ..common import ensure_root
from ..config.config import remove_cache_db
from ..config.store import SettingsError, active_profile, invoking_user, settings_query
from .manager import ServiceManager

DAEMON_SOCKET = Path("/run/sing-box.socket")
DAEMON_UNIT = Path("/usr/lib/systemd/system/sing-box-daemon.service")

_TIMEOUT = 15
_LIFECYCLE_TIMEOUT = 15
_DAEMON_SERVICE = "sing-box-daemon.service"
_METADATA = (("accept-language", "en-US"),)

_GET_DAEMON_INFO = "/desktop.DesktopService/GetDaemonInfo"
_START_SERVICE = "/desktop.DesktopService/StartService"
_GET_WORKING_DIRECTORY = "/desktop.DesktopService/GetWorkingDirectory"
_DESTROY_WORKING_DIRECTORY = "/desktop.DesktopService/DestroyWorkingDirectory"
_STOP_SERVICE = "/daemon.ManagedService/StopService"
_SUBSCRIBE_SERVICE_STATUS = "/daemon.StartedService/SubscribeServiceStatus"

_START_SERVICE_REQUEST_CONFIG_CONTENT = 1
_START_SERVICE_REQUEST_OPTIONS = 2
_START_OPTIONS_OOM_KILLER_ENABLED = 1
_START_OPTIONS_OOM_KILLER_DISABLED = 2
_START_OPTIONS_OOM_MEMORY_LIMIT = 3
_START_OPTIONS_POWER_REPORT_ENABLED = 4


class DaemonOwnership(IntEnum):
    AVAILABLE = 1
    CALLER = 2
    OTHER = 3


class ServiceStatusType(IntEnum):
    IDLE = 0
    STARTING = 1
    STARTED = 2
    STOPPING = 3
    FATAL = 4


_STATUS_NAMES: dict[int, str] = {
    ServiceStatusType.IDLE: "Stopped",
    ServiceStatusType.STARTING: "Starting",
    ServiceStatusType.STARTED: "Running",
    ServiceStatusType.STOPPING: "Stopping",
    ServiceStatusType.FATAL: "Fatal",
}


@dataclass(frozen=True)
class StartOptions:
    """KEEP: the daemon has no RPC that returns the running StartOptions, so they are read from the app's preferences (https://github.com/SagerNet/sing-box/blob/v1.14.4/experimental/boxdd/desktop_service.proto#L9-L38)."""

    oom_killer_enabled: bool = False
    oom_killer_disabled: bool = True
    oom_memory_limit_mb: int = 50
    power_report_enabled: bool = False


class DesktopDaemonError(Exception): ...


class DesktopDaemonUnavailable(DesktopDaemonError): ...


def desktop_daemon_available() -> bool:
    try:
        return stat.S_ISSOCK(DAEMON_SOCKET.stat().st_mode)
    except OSError:
        return False


def desktop_app_installed() -> bool:
    if sys.platform == "win32":
        from .desktop_windows import desktop_app_installed as windows_app_installed

        return windows_app_installed()
    return DAEMON_UNIT.exists() or desktop_daemon_available()


def read_start_options(config_dir: Path) -> StartOptions:
    names = (
        "oom_killer_enabled",
        "oom_killer_kill_connections",
        "oom_memory_limit_mb",
        "power_report_enabled",
    )
    rows = settings_query(
        config_dir,
        f"SELECT name, data FROM preferences WHERE name IN ({','.join('?' * len(names))})",
        names,
    )
    values: dict[str, object] = {}
    for name, data in rows:
        try:
            values[name] = json.loads(bytes(data))
        except (TypeError, ValueError) as e:
            raise SettingsError(
                f"The sing-box app's preference {name} is not valid JSON."
            ) from e

    def flag(name: str, default: bool) -> bool:
        value = values.get(name)
        return value if isinstance(value, bool) else default

    limit = values.get("oom_memory_limit_mb")
    limit_mb = 50
    if isinstance(limit, int | float) and not isinstance(limit, bool) and limit > 0:
        limit_mb = int(limit)
    return StartOptions(
        oom_killer_enabled=flag("oom_killer_enabled", False),
        oom_killer_disabled=not flag("oom_killer_kill_connections", False),
        oom_memory_limit_mb=limit_mb,
        power_report_enabled=flag("power_report_enabled", False),
    )


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def encode_start_request(
    config_content: str, options: StartOptions | None = None
) -> bytes:
    opts = options or StartOptions()
    body = b""
    if opts.oom_killer_enabled:
        body += _field_varint(_START_OPTIONS_OOM_KILLER_ENABLED, 1)
    if opts.oom_killer_disabled:
        body += _field_varint(_START_OPTIONS_OOM_KILLER_DISABLED, 1)
    body += _field_varint(
        _START_OPTIONS_OOM_MEMORY_LIMIT, opts.oom_memory_limit_mb * 1024 * 1024
    )
    if opts.power_report_enabled:
        body += _field_varint(_START_OPTIONS_POWER_REPORT_ENABLED, 1)
    return _field_bytes(
        _START_SERVICE_REQUEST_CONFIG_CONTENT, config_content.encode("utf-8")
    ) + _field_bytes(_START_SERVICE_REQUEST_OPTIONS, body)


def decode_fields(data: bytes) -> dict[int, int | bytes]:
    fields: dict[int, int | bytes] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            fields[number], pos = _read_varint(data, pos)
        elif wire_type == 2:
            size, pos = _read_varint(data, pos)
            fields[number] = data[pos : pos + size]
            pos += size
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    return fields


def _as_text(value: int | bytes | None) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else ""


def _as_int(value: int | bytes | None) -> int:
    return value if isinstance(value, int) else 0


class DesktopDaemonServiceManager(ServiceManager):
    requires_root = False
    # KEEP: StartService on a running core reloads it in place (https://github.com/SagerNet/sing-box/blob/v1.14.4/experimental/boxdd/server.go#L239)
    restarts_in_place = True
    supports_purge_data = True
    _rpc_deadline: float | None = None

    @contextmanager
    def _rpc_budget(self, seconds: float) -> Iterator[None]:
        previous = self._rpc_deadline
        self._rpc_deadline = time.monotonic() + seconds
        try:
            yield
        finally:
            self._rpc_deadline = previous

    @contextmanager
    def _lifecycle_guard(self) -> Iterator[None]:
        from .debug import DebugTakeoverError

        ensure_root()
        try:
            yield
        except (DesktopDaemonError, SettingsError, DebugTakeoverError, OSError) as e:
            print(f"❌ {e}")
            raise typer.Exit(1) from e

    @contextmanager
    def _guard(self) -> Iterator[None]:
        try:
            with invoking_user():
                yield
        except (DesktopDaemonError, SettingsError) as e:
            print(f"❌ {e}")
            raise typer.Exit(1)

    def _call(
        self, method: str, request: bytes = b"", *, stream: bool = False
    ) -> bytes:
        if not desktop_daemon_available():
            raise DesktopDaemonUnavailable(
                "The sing-box desktop daemon is not running, start it with: "
                "sudo sbc service enable"
            )
        timeout = float(_TIMEOUT)
        if self._rpc_deadline is not None:
            timeout = min(timeout, self._rpc_deadline - time.monotonic())
            if timeout <= 0:
                raise DesktopDaemonUnavailable(
                    "Desktop lifecycle RPC deadline exceeded."
                )
        try:
            import grpc  # type: ignore[import-not-found,import-untyped]
        except ImportError as e:
            raise DesktopDaemonError(
                "sing-box desktop app detected, which needs grpcio: "
                "pip install 'sing-box-cli[desktop]'"
            ) from e

        try:
            with grpc.insecure_channel(f"unix:{DAEMON_SOCKET}") as channel:
                if stream:
                    replies: Any = channel.unary_stream(method)(
                        request, metadata=_METADATA, timeout=timeout
                    )
                    try:
                        return bytes(next(iter(replies)))
                    finally:
                        replies.cancel()
                return bytes(
                    channel.unary_unary(method)(
                        request, metadata=_METADATA, timeout=timeout
                    )
                )
        except grpc.RpcError as e:
            hint = ""
            if sys.platform == "linux" and e.code() in (
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.UNAVAILABLE,
            ):
                hint = (
                    "\nFor foreground debugging, stop the desktop app and services with: "
                    "sudo sbc run --force-cli"
                )
            error = DesktopDaemonUnavailable if hint else DesktopDaemonError
            raise error(
                f"sing-box daemon call failed: {e.code().name} {e.details()}{hint}"
            ) from e
        except StopIteration as e:
            raise DesktopDaemonError("sing-box daemon returned no status") from e

    def _daemon_info(self) -> dict[int, int | bytes]:
        return decode_fields(self._call(_GET_DAEMON_INFO))

    def _ensure_owner(self) -> None:
        if sys.platform != "win32" and os.geteuid() == 0:
            raise DesktopDaemonError(
                "The desktop daemon only accepts its owning user, "
                "run as that user (or with sudo from it)."
            )
        ownership = _as_int(self._daemon_info().get(2))
        if ownership == DaemonOwnership.AVAILABLE:
            raise DesktopDaemonError(
                "The desktop daemon has no owner yet, open the sing-box app once."
            )
        if ownership == DaemonOwnership.OTHER:
            raise DesktopDaemonError("The desktop daemon is owned by another user.")

    def _service_status(self) -> tuple[int, str]:
        reply = decode_fields(self._call(_SUBSCRIBE_SERVICE_STATUS, stream=True))
        return _as_int(reply.get(1)), _as_text(reply.get(2))

    def _start_request(self) -> bytes:
        config_dir = self.config.config_dir
        profile = active_profile(config_dir)
        return encode_start_request(
            profile.path.read_text(encoding="utf-8"), read_start_options(config_dir)
        )

    def create_service(self) -> None:
        print("ℹ️ Autostart is managed by the desktop app's sing-box-daemon.service.")

    def _wait_ready(self) -> None:
        from . import debug

        deadline = time.monotonic() + _LIFECYCLE_TIMEOUT
        last_error = "daemon RPC is not ready"
        state: dict[str, str] = {}
        print("⏳ Waiting for desktop daemon RPC readiness.")
        while (remaining := deadline - time.monotonic()) > 0:
            state = debug._unit_state(
                _DAEMON_SERVICE, timeout=min(debug._SYSTEMCTL_TIMEOUT, remaining)
            )
            if state["LoadState"] != "loaded":
                raise DesktopDaemonError(
                    f"Desktop daemon unit is not loaded: {state['LoadState']}"
                )
            if state["ActiveState"] == "failed" or (
                state["ActiveState"] == "inactive" and state["Job"] in ("", "0")
            ):
                raise DesktopDaemonError(
                    f"Desktop daemon startup failed: ActiveState={state['ActiveState']}; "
                    "inspect journalctl -u sing-box-daemon.service."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if state["ActiveState"] == "active":
                try:
                    with invoking_user(), self._rpc_budget(min(1, remaining)):
                        self._ensure_owner()
                    return
                except DesktopDaemonUnavailable as e:
                    last_error = str(e)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        phase = (
            "Desktop daemon is running but RPC is unresponsive"
            if state.get("ActiveState") == "active"
            else "Desktop daemon did not become ready"
        )
        raise DesktopDaemonError(f"{phase} within {_LIFECYCLE_TIMEOUT}s: {last_error}")

    def enable_service(self) -> None:
        if sys.platform != "linux":
            return super().enable_service()
        from . import debug

        with self._lifecycle_guard():
            with invoking_user():
                request = self._start_request()
            state = debug._unit_state(_DAEMON_SERVICE)
            if state["LoadState"] != "loaded":
                raise DesktopDaemonError(
                    f"Desktop daemon unit is not loaded: {state['LoadState']}"
                )
            if state["ActiveState"] not in ("active", "activating"):
                print("⏳ Desktop daemon is not running; starting it.")
                debug._systemctl(["--no-block", "start", _DAEMON_SERVICE])
            self._wait_ready()
            print("⏳ Starting core with the selected desktop profile.")
            with invoking_user():
                self._call(_START_SERVICE, request)

    def _stop_core_verified(self) -> None:
        with invoking_user(), self._rpc_budget(_LIFECYCLE_TIMEOUT):
            self._ensure_owner()
            if self._service_status()[0] != ServiceStatusType.IDLE:
                print("⏳ Stopping desktop core.")
                self._call(_STOP_SERVICE)
                print("⏳ Verifying desktop core is stopped.")
                while self._service_status()[0] != ServiceStatusType.IDLE:
                    remaining = (self._rpc_deadline or 0) - time.monotonic()
                    if remaining <= 0:
                        raise DesktopDaemonError("Core stop verification timed out.")
                    time.sleep(min(0.1, remaining))
        print("✋ Core stopped (verified through RPC).")

    def disable_service(self) -> str:
        if sys.platform != "linux":
            return super().disable_service()
        from . import debug

        with self._lifecycle_guard():
            try:
                self._stop_core_verified()
            except DesktopDaemonError as e:
                print(f"⚠️ Cannot confirm core shutdown through RPC: {e}")
                print("⏳ Continuing with system-level desktop daemon shutdown.")
            debug._stop_units((_DAEMON_SERVICE,))
        return "Core and desktop daemon stopped."

    def check_service(self) -> bool:
        return True

    def start(self) -> None:
        with self._guard():
            self._ensure_owner()
            self._call(_START_SERVICE, self._start_request())

    def stop(self) -> None:
        with self._guard():
            self._ensure_owner()
            if self._service_status()[0] != ServiceStatusType.IDLE:
                self._call(_STOP_SERVICE)

    def restart(self) -> None:
        self.start()

    def clear_cache(self) -> None:
        """KEEP: the daemon has no RPC to delete a single file and its working directory is root-only, so the cache file is removed as root (https://github.com/SagerNet/sing-box/blob/v1.14.4/experimental/boxdd/desktop_service.proto#L14-L15, #51)."""
        with self._guard():
            self._ensure_owner()
            info = decode_fields(self._call(_GET_WORKING_DIRECTORY))
            if not (path := _as_text(info.get(1))):
                raise DesktopDaemonError(
                    "sing-box daemon returned no working directory"
                )
            directory = Path(path)
            was_running = self._service_status()[0] != ServiceStatusType.IDLE
        ensure_root()
        # KEEP: the core resolves the cache path against its working directory and refuses paths outside it unless insecure mode is on (https://github.com/SagerNet/sing-box/blob/v1.14.4/experimental/boxdd/insecure_mode_supported.go#L54-L105)
        cache = (directory / self.config.cache_file_path).resolve()
        if not cache.is_relative_to(directory.resolve()):
            print(f"❌ {cache} is outside the daemon's working directory, not removed.")
            raise typer.Exit(1)
        if was_running:
            with self._guard():
                self._call(_STOP_SERVICE)
        remove_cache_db(cache)
        if was_running:
            with self._guard():
                self._call(_START_SERVICE, self._start_request())

    def purge_data(self) -> None:
        with self._guard():
            self._ensure_owner()
            self._call(_DESTROY_WORKING_DIRECTORY)
            print("🗑️ Daemon working directory cleared.")

    def status(self) -> str:
        with self._guard():
            self._ensure_owner()
            code, message = self._service_status()
            name = _STATUS_NAMES.get(code, f"Unknown({code})")
            return f"{name}: {message}" if message else name
        return ""

    def disable(self) -> None:
        print("ℹ️ Autostart is managed by the desktop app's sing-box-daemon.service.")

    def version(self) -> str:
        with self._guard():
            return f"sing-box {_as_text(self._daemon_info().get(1))} (desktop daemon)"
        return ""
