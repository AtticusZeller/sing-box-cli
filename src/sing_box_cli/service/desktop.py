"""Service backend for the sing-box desktop app (Linux).

The desktop app installs ``sing-box-daemon.service``, a root daemon that exposes a
gRPC API on a unix socket. This backend drives that daemon instead of creating a
systemd unit of its own, which would fight the daemon over TUN and the API port.

``grpcio`` is optional (``sing-box-cli[desktop]``) and imported lazily, so machines
without the desktop app never load it. Messages are tiny, so the protobuf wire
format is encoded by hand instead of depending on ``protobuf``.

The daemon only talks to the user who owns it, identified by uid. Under ``sudo``
everything here therefore runs as the invoking user (:func:`invoking_user`). The
profile and the start options come from the app's settings (``config.store``).
"""

import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from ..config.store import SettingsError, active_profile, invoking_user, settings_query
from .manager import ServiceManager

DAEMON_SOCKET = Path("/run/sing-box.socket")
DAEMON_UNIT = Path("/usr/lib/systemd/system/sing-box-daemon.service")

_TIMEOUT = 15
_METADATA = (("accept-language", "en-US"),)

_GET_DAEMON_INFO = "/desktop.DesktopService/GetDaemonInfo"
_START_SERVICE = "/desktop.DesktopService/StartService"
_DESTROY_WORKING_DIRECTORY = "/desktop.DesktopService/DestroyWorkingDirectory"
_STOP_SERVICE = "/daemon.ManagedService/StopService"
_SUBSCRIBE_SERVICE_STATUS = "/daemon.StartedService/SubscribeServiceStatus"

# desktop.DaemonOwnership
_OWNERSHIP_AVAILABLE = 1
_OWNERSHIP_OTHER = 3

# daemon.ServiceStatus.Type
_STATUS_NAMES = {0: "Stopped", 1: "Starting", 2: "Running", 3: "Stopping", 4: "Fatal"}
_STATUS_IDLE = 0


@dataclass(frozen=True)
class StartOptions:
    """Start options of the service, the app keeps them in its preferences.

    The daemon cannot report the options of the running service, so they are read
    from the same preferences the app uses, with the app's own defaults.
    """

    oom_killer_enabled: bool = False
    # the app's "kill connections" preference, inverted
    oom_killer_disabled: bool = True
    oom_memory_limit_mb: int = 50
    power_report_enabled: bool = False


class DesktopDaemonError(Exception):
    """The desktop daemon cannot be driven; the message tells the user why."""


def desktop_daemon_available() -> bool:
    """Whether the daemon is listening on its socket."""
    try:
        return stat.S_ISSOCK(DAEMON_SOCKET.stat().st_mode)
    except OSError:
        return False


def desktop_app_installed() -> bool:
    """Whether the desktop app is on this machine, running or not.

    Used to pick this backend, so a stopped daemon is reported instead of falling
    back to a systemd unit of our own that would fight it. Two stats, no imports.
    """
    return DAEMON_UNIT.exists() or desktop_daemon_available()


def read_start_options(config_dir: Path) -> StartOptions:
    """The start options set in the app. A preference never set has the app's default."""
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
    """desktop.StartServiceRequest{config_content, options: desktop.StartOptions}"""
    opts = options or StartOptions()
    body = b""
    if opts.oom_killer_enabled:
        body += _field_varint(1, 1)
    if opts.oom_killer_disabled:
        body += _field_varint(2, 1)
    body += _field_varint(3, opts.oom_memory_limit_mb * 1024 * 1024)
    if opts.power_report_enabled:
        body += _field_varint(4, 1)
    return _field_bytes(1, config_content.encode("utf-8")) + _field_bytes(2, body)


def decode_fields(data: bytes) -> dict[int, int | bytes]:
    """Decode a flat protobuf message into ``{field number: varint | bytes}``."""
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
    # The daemon only accepts its owning user, sudo is handled by invoking_user().
    requires_root = False
    # Starting a running service reloads it in place, no stop needed.
    hot_reload = True

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
            raise DesktopDaemonError(
                "The sing-box desktop daemon is not running, start it with: "
                "sudo systemctl start sing-box-daemon.service"
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
                        request, metadata=_METADATA, timeout=_TIMEOUT
                    )
                    try:
                        return bytes(next(iter(replies)))
                    finally:
                        replies.cancel()
                return bytes(
                    channel.unary_unary(method)(
                        request, metadata=_METADATA, timeout=_TIMEOUT
                    )
                )
        except grpc.RpcError as e:
            raise DesktopDaemonError(
                f"sing-box daemon call failed: {e.code().name} {e.details()}"
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
        if ownership == _OWNERSHIP_AVAILABLE:
            raise DesktopDaemonError(
                "The desktop daemon has no owner yet, open the sing-box app once."
            )
        if ownership == _OWNERSHIP_OTHER:
            raise DesktopDaemonError("The desktop daemon is owned by another user.")

    def _service_status(self) -> tuple[int, str]:
        reply = decode_fields(self._call(_SUBSCRIBE_SERVICE_STATUS, stream=True))
        return _as_int(reply.get(1)), _as_text(reply.get(2))

    def _start_request(self) -> bytes:
        """The active profile with the start options configured in the app."""
        config_dir = self.config.config_dir
        profile = active_profile(config_dir)
        return encode_start_request(
            profile.path.read_text(encoding="utf-8"), read_start_options(config_dir)
        )

    def create_service(self) -> None:
        print("ℹ️ Autostart is managed by the desktop app's sing-box-daemon.service.")

    def check_service(self) -> bool:
        return True

    def start(self) -> None:
        # Starting while running is a hot reload, same as the desktop app does.
        with self._guard():
            self._ensure_owner()
            self._call(_START_SERVICE, self._start_request())

    def stop(self) -> None:
        with self._guard():
            self._ensure_owner()
            if self._service_status()[0] != _STATUS_IDLE:
                self._call(_STOP_SERVICE)

    def restart(self) -> None:
        self.start()

    def clear_cache(self) -> None:
        """Clear the daemon's working directory, as "clear data" in the desktop app.

        The cache database lives in a root-owned directory only the daemon can
        remove. The service must be stopped first and is brought back if it ran.
        """
        with self._guard():
            self._ensure_owner()
            was_running = self._service_status()[0] != _STATUS_IDLE
            if was_running:
                self._call(_STOP_SERVICE)
            self._call(_DESTROY_WORKING_DIRECTORY)
            print("🗑️ Cache database removed.")
            if was_running:
                self._call(_START_SERVICE, self._start_request())

    def status(self) -> str:
        with self._guard():
            self._ensure_owner()
            code, message = self._service_status()
            name = _STATUS_NAMES.get(code, f"Unknown({code})")
            return f"{name}: {message}" if message else name
        return ""  # unreachable, _guard exits

    def disable(self) -> None:
        print("ℹ️ Autostart is managed by the desktop app's sing-box-daemon.service.")

    def version(self) -> str:
        with self._guard():
            return f"sing-box {_as_text(self._daemon_info().get(1))} (desktop daemon)"
        return ""  # unreachable, _guard exits
