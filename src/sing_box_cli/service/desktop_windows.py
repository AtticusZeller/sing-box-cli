"""Windows desktop installation layout and service ownership.

The official daemon authenticates the installed, signed application and its
worker, including their parent process relationship. A Python client cannot use
that private named-pipe API. Share the profile store, but never start a competing
SCM service or claim that a desktop service operation succeeded.
"""

import sys
from pathlib import Path

from ..config.store import SettingsError
from .desktop import DesktopDaemonError, DesktopDaemonServiceManager

INSTALLATION_KEY = r"SOFTWARE\SagerNet\sing-box"
SERVICE_KEY = r"SYSTEM\CurrentControlSet\Services\sing-box-daemon"


def _registry_values(key: str, names: tuple[str, ...]) -> dict[str, object] | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as handle:
            values: dict[str, object] = {}
            for name in names:
                try:
                    values[name] = winreg.QueryValueEx(handle, name)[0]
                except FileNotFoundError:
                    values[name] = None
            return values
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SettingsError(
            f"Cannot read the sing-box installation ({key}): {e}"
        ) from e


def application_data_directory(default: Path) -> Path:
    """Use the installer's layout v2 override, like the packaged desktop app.

    Missing layout metadata is an older installation using %APPDATA%/sing-box.
    Invalid metadata is an error, never permission to initialize another store.
    Environment variables in registry strings are not expanded by the app either.
    """
    values = _registry_values(
        INSTALLATION_KEY,
        ("LayoutVersion", "ApplicationDataDirectory", "DaemonDataDirectory"),
    )
    if values is None or values["LayoutVersion"] is None:
        return default
    if values["LayoutVersion"] != 2:
        raise SettingsError("Unsupported sing-box installation layout version.")
    application = values["ApplicationDataDirectory"]
    daemon = values["DaemonDataDirectory"]
    if (
        not isinstance(application, str)
        or not isinstance(daemon, str)
        or not daemon.strip()
    ):
        raise SettingsError("The sing-box installation layout is invalid.")
    if not application.strip():
        return default
    path = Path(application)
    if not path.is_absolute():
        raise SettingsError("The sing-box application data directory must be absolute.")
    return path


def desktop_app_installed() -> bool:
    """Detect an installed desktop app even when its daemon is stopped."""
    return (
        _registry_values(SERVICE_KEY, ()) is not None
        or _registry_values(INSTALLATION_KEY, ()) is not None
    )


class WindowsDesktopServiceManager(DesktopDaemonServiceManager):
    """Keep desktop-owned commands out of the standalone Windows service backend."""

    def _call(
        self, method: str, request: bytes = b"", *, stream: bool = False
    ) -> bytes:
        raise DesktopDaemonError(
            "The Windows sing-box desktop daemon only accepts its signed desktop "
            "application and worker; service control from sbc is unavailable. "
            "Use the desktop app to start, stop, reload or clear data. "
            "Configuration changes are saved by `sbc config update` without --restart."
        )

    def create_service(self) -> None:
        with self._guard():
            self._call("")

    def disable(self) -> None:
        with self._guard():
            self._call("")
