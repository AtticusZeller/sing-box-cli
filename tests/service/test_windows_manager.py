import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sing_box_cli.config.config import ConfigHandler
from sing_box_cli.service.manager import WindowsServiceManager


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WindowsServiceManager:
    config = MagicMock(spec=ConfigHandler)
    config.bin_path = Path("C:/Program Files/sing-box/sing-box.exe")
    config.config_dir = tmp_path / "Test User" / "代理配置"
    config.config_dir.mkdir(parents=True)
    config.config_file = config.config_dir / "config.json"
    monkeypatch.setattr(
        WindowsServiceManager, "nssm_bin", property(lambda _self: "nssm.exe")
    )
    return WindowsServiceManager(config)


def mock_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exists: bool = True,
    state: str = "SERVICE_STOPPED",
    fail: str | None = None,
    query_error: int | None = None,
) -> MagicMock:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "sc.exe":
            return subprocess.CompletedProcess(
                command, query_error or (0 if exists else 1060), "query output", ""
            )
        result = subprocess.CompletedProcess(
            command,
            5 if command[1] == fail else 0,
            state + "\n" if command[1] == "status" else "",
            "Access denied" if command[1] == fail else "",
        )
        if kwargs.get("check"):
            result.check_returncode()
        return result

    mock_run = MagicMock(side_effect=run)
    monkeypatch.setattr(subprocess, "run", mock_run)
    return mock_run


def test_install_preserves_paths_with_spaces(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = mock_commands(monkeypatch, exists=False)
    manager.create_service()
    install = next(call for call in run.call_args_list if call.args[0][1] == "install")
    assert install.args[0] == [
        "nssm.exe",
        "install",
        manager.service_name,
        str(manager.config.bin_path),
        "run",
        "-c",
        str(manager.config.config_file),
        "-D",
        str(manager.config.config_dir),
    ]


@pytest.mark.parametrize(
    "method,command,state,exists",
    [
        ("create_service", "install", "SERVICE_STOPPED", False),
        ("create_service", "set", "SERVICE_STOPPED", False),
        ("start", "start", "SERVICE_STOPPED", True),
        ("stop", "stop", "SERVICE_RUNNING", True),
        ("disable", "remove", "SERVICE_STOPPED", True),
        ("restart", "restart", "SERVICE_RUNNING", True),
    ],
)
def test_command_errors_propagate(
    manager: WindowsServiceManager,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    command: str,
    state: str,
    exists: bool,
) -> None:
    run = mock_commands(monkeypatch, exists=exists, state=state, fail=command)
    with pytest.raises(subprocess.CalledProcessError) as error:
        getattr(manager, method)()
    assert error.value.returncode == 5
    assert error.value.cmd[1] == command
    if command == "install":
        assert not any(call.args[0][1] == "set" for call in run.call_args_list)


def test_existing_service_is_not_reinstalled(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = mock_commands(monkeypatch)
    manager.create_service()
    assert not any(call.args[0][1] == "install" for call in run.call_args_list)
    assert any(call.args[0][1] == "set" for call in run.call_args_list)
    # Existing installations must receive the fixed paths too.
    run.assert_any_call(
        [
            "nssm.exe",
            "set",
            manager.service_name,
            "Application",
            str(manager.config.bin_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    parameters = next(
        call.args[0][4]
        for call in run.call_args_list
        if call.args[0][1:4] == ["set", manager.service_name, "AppParameters"]
    )
    assert parameters == subprocess.list2cmdline(
        [
            "run",
            "-c",
            str(manager.config.config_file),
            "-D",
            str(manager.config.config_dir),
        ]
    )


def test_service_uses_user_config_without_log_overrides(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = '{"log": {"level": "debug"}}'
    manager.config.config_file.write_text(original, encoding="utf-8")
    mock_commands(monkeypatch, exists=False)
    manager.create_service()
    assert manager.config.config_file.read_text(encoding="utf-8") == original
    assert list(manager.config.config_dir.iterdir()) == [manager.config.config_file]


@pytest.mark.parametrize(
    "method,state,exists,command",
    [
        ("start", "SERVICE_RUNNING", True, "start"),
        ("stop", "SERVICE_STOPPED", True, "stop"),
        ("stop", "SERVICE_STOPPED", False, "stop"),
        ("disable", "SERVICE_STOPPED", False, "remove"),
    ],
)
def test_repeated_operations_are_noops(
    manager: WindowsServiceManager,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    state: str,
    exists: bool,
    command: str,
) -> None:
    run = mock_commands(monkeypatch, exists=exists, state=state)
    getattr(manager, method)()
    assert not any(call.args[0][1] == command for call in run.call_args_list)


@pytest.mark.parametrize("state", ["SERVICE_START_PENDING", "SERVICE_STOP_PENDING"])
def test_pending_service_still_exists(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    mock_commands(monkeypatch, state=state)
    assert manager.check_service()


def test_missing_service(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_commands(monkeypatch, exists=False, state="")
    assert not manager.check_service()
    assert manager.status() == "Service not installed"


@pytest.mark.parametrize("method", ["check_service", "create_service", "status"])
def test_query_failure_is_not_treated_as_missing(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    mock_commands(monkeypatch, state="", query_error=5)
    with pytest.raises(subprocess.CalledProcessError):
        getattr(manager, method)()


def test_status_failure_propagates(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_commands(monkeypatch, fail="status")
    with pytest.raises(subprocess.CalledProcessError):
        manager.status()


def test_status_display(
    manager: WindowsServiceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_commands(monkeypatch, state="SERVICE_RUNNING")
    assert manager.status() == "Service Running"
