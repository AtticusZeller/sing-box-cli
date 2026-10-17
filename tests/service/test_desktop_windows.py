"""Windows desktop layout, store ownership and unsupported service controls."""

import importlib
import subprocess
import sys
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
import typer
from typer.testing import CliRunner

from sing_box_cli.config.store import SettingsError
from sing_box_cli.service import desktop, desktop_windows as windows

config_module = importlib.import_module("sing_box_cli.config.config")
REGISTRY_READER = windows._registry_values


@pytest.mark.parametrize("key", [windows.SERVICE_KEY, windows.INSTALLATION_KEY])
def test_installed_app_is_detected_without_a_running_daemon(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(
        windows, "_registry_values", lambda path, _: {} if path == key else None
    )
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.desktop_app_installed()


def test_no_registry_metadata_uses_the_default_directory(tmp_path: Path) -> None:
    assert not windows.desktop_app_installed()
    assert windows.application_data_directory(tmp_path) == tmp_path


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    values: dict[str, object] = {
        "LayoutVersion": 2,
        "ApplicationDataDirectory": str(tmp_path / "shared data"),
        "DaemonDataDirectory": str(tmp_path / "daemon"),
    }
    monkeypatch.setattr(windows, "_registry_values", lambda *_: values)
    return values


def test_installer_directory_overrides_appdata(
    tmp_path: Path, layout: dict[str, object]
) -> None:
    assert windows.application_data_directory(tmp_path) == tmp_path / "shared data"
    assert not (tmp_path / "shared data").exists()  # reading never creates it

    layout["ApplicationDataDirectory"] = ""
    assert windows.application_data_directory(tmp_path) == tmp_path
    layout["LayoutVersion"] = None  # older installer
    assert windows.application_data_directory(tmp_path) == tmp_path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LayoutVersion", 3),
        ("ApplicationDataDirectory", None),
        ("ApplicationDataDirectory", "relative/path"),
        ("DaemonDataDirectory", ""),
        ("DaemonDataDirectory", 42),
    ],
)
def test_invalid_layout_is_reported(
    tmp_path: Path, layout: dict[str, object], name: str, value: object
) -> None:
    layout[name] = value
    with pytest.raises(SettingsError):
        windows.application_data_directory(tmp_path)


def test_config_uses_the_selected_profile_in_the_installer_directory(
    tmp_path: Path,
    layout: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    make_settings_db: Callable[..., Path],
) -> None:
    directory = tmp_path / "shared data"
    (directory / "profiles").mkdir(parents=True)
    profile = directory / "profiles" / "p.json"
    profile.write_text("{}")
    make_settings_db(directory, selected="p", profiles={"p": "https://sub.example/p"})
    monkeypatch.setattr(
        config_module.ConfigHandler, "is_windows", property(lambda _: True)
    )
    monkeypatch.setattr(
        config_module.typer, "get_app_dir", lambda *a, **kw: str(tmp_path / "unused")
    )
    monkeypatch.setattr(desktop, "desktop_app_installed", windows.desktop_app_installed)

    handler = config_module.get_config()

    assert handler.external_config
    assert handler.config_file == profile
    assert handler.sub_url == "https://sub.example/p"
    assert not (tmp_path / "unused").exists()


def test_missing_app_store_is_never_created(
    tmp_path: Path, layout: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config_module.ConfigHandler, "is_windows", property(lambda _: True)
    )
    monkeypatch.setattr(desktop, "desktop_app_installed", windows.desktop_app_installed)
    handler = config_module.get_config()

    assert handler.external_config
    with pytest.raises(typer.Exit):
        _ = handler.config_file_content
    assert not (tmp_path / "shared data").exists()


def test_registry_errors_are_not_treated_as_no_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            OpenKey=denied, HKEY_LOCAL_MACHINE=0, KEY_READ=1, KEY_WOW64_64KEY=2
        ),
    )
    # Replace the test suite's default stub with the real reader.
    monkeypatch.setattr(windows, "_registry_values", REGISTRY_READER)
    with pytest.raises(SettingsError, match="Cannot read"):
        windows.application_data_directory(tmp_path)


def test_registry_reader_uses_native_view_and_preserves_literal_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(windows.sys, "platform", "win32")

    def open_key(*args: object) -> Any:
        calls.append(args)
        return nullcontext("handle")

    def query_value(handle: object, name: str) -> tuple[object, int]:
        if name == "missing":
            raise FileNotFoundError
        return r"%APPDATA%\sing-box", 2  # REG_EXPAND_SZ: the app does not expand it

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            OpenKey=open_key,
            QueryValueEx=query_value,
            HKEY_LOCAL_MACHINE=0,
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
        ),
    )
    assert REGISTRY_READER(windows.INSTALLATION_KEY, ("path", "missing")) == {
        "path": r"%APPDATA%\sing-box",
        "missing": None,
    }
    assert calls == [(0, windows.INSTALLATION_KEY, 0, 3)]


@pytest.mark.parametrize(
    "action",
    ["start", "stop", "restart", "status", "clear_cache", "create_service", "disable"],
)
def test_desktop_commands_report_authentication_limit_without_starting_another_service(
    action: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("desktop mode must not invoke the standalone service helper")

    monkeypatch.setattr("subprocess.run", unexpected)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    service = windows.WindowsDesktopServiceManager(SimpleNamespace())
    with pytest.raises(typer.Exit) as error:
        getattr(service, action)()
    assert error.value.exit_code == 1
    output = capsys.readouterr().out
    assert "signed desktop application and worker" in output
    assert "systemctl" not in output


@pytest.fixture
def daemon_executable(monkeypatch: pytest.MonkeyPatch) -> str:
    """Only inspect the official service; no profile, API or bundled core is needed."""
    executable = r"C:\Program Files\sing-box\resources\daemon\sing-box-daemon.exe"
    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setattr(
        windows.psutil,
        "win_service_get",
        lambda name: SimpleNamespace(pid=lambda: 123),
        raising=False,
    )
    monkeypatch.setattr(
        windows.psutil, "Process", lambda pid: SimpleNamespace(exe=lambda: executable)
    )
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    return executable


def test_version_reads_the_desktop_daemon_without_an_api_or_profile(
    daemon_executable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, "sing-box-daemon version 1.14.3\ncore api version 1\n"
        )

    monkeypatch.setattr(subprocess, "run", run)
    config = SimpleNamespace()  # deliberately has no API address or bin_path
    service = windows.WindowsDesktopServiceManager(config)
    main = importlib.import_module("sing_box_cli.main")
    monkeypatch.setattr(main, "get_config", lambda: config)
    monkeypatch.setattr(main, "create_service", lambda _: service)
    result = CliRunner().invoke(main.app, ["version"])

    assert result.exit_code == 0, result.output
    assert "sing-box 1.14.3 (desktop daemon)" in result.output
    assert calls == [
        (
            [daemon_executable, "version"],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "timeout": 15,
                "creationflags": subprocess.CREATE_NO_WINDOW,
            },
        )
    ]


@pytest.mark.parametrize(
    "output", ["", "core api version 1\n", "sing-box-daemon version \n"]
)
def test_version_does_not_invent_a_version(
    daemon_executable: str, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, output)
    )
    with pytest.raises(typer.Exit):
        windows.WindowsDesktopServiceManager(SimpleNamespace()).version()


@pytest.mark.parametrize(
    "error",
    [
        psutil.AccessDenied(123),
        psutil.NoSuchProcess(123),
        FileNotFoundError("not found"),
        subprocess.TimeoutExpired("version", 15),
        subprocess.CalledProcessError(1, "version", stderr="private diagnostic"),
    ],
)
def test_version_lookup_failures_are_reported_without_a_traceback(
    daemon_executable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    if isinstance(error, psutil.Error):
        monkeypatch.setattr(windows.psutil, "Process", fail)
    else:
        monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(typer.Exit):
        windows.WindowsDesktopServiceManager(SimpleNamespace()).version()
    output = capsys.readouterr().out
    assert "Cannot read" in output
    assert "private diagnostic" not in output


def test_version_reports_a_stopped_daemon_without_starting_it(
    daemon_executable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        windows.psutil, "win_service_get", lambda _: SimpleNamespace(pid=lambda: None)
    )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("version must not start the daemon or another core")

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(typer.Exit):
        windows.WindowsDesktopServiceManager(SimpleNamespace()).version()
    assert "not running" in capsys.readouterr().out
