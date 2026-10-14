"""The service commands against a recording backend."""

from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from sing_box_cli.service import SharedContext, service as service_app
from sing_box_cli.service.manager import ServiceManager


class Recorder(ServiceManager):
    def __init__(self, *, installed: bool = True) -> None:
        config: Any = SimpleNamespace(api_base_url="", update_config=lambda: True)
        super().__init__(config)
        self.installed = installed
        self.calls: list[str] = []
        config.clear_cache = lambda: self.calls.append("config.clear_cache")

    def check_service(self) -> bool:
        return self.installed

    def create_service(self) -> None:
        self.calls.append("create_service")

    def sync_service(self) -> None:
        self.calls.append("sync_service")

    def stop(self) -> None:
        self.calls.append("stop")

    def start(self) -> None:
        self.calls.append("start")

    def restart(self) -> None:
        self.calls.append("restart")

    def status(self) -> str:
        return "Running"


class ReloadingRecorder(Recorder):
    requires_root = False
    hot_reload = True


def invoke(service: ServiceManager, *args: str) -> None:
    obj = SharedContext(config=service.config, service=service)
    result = CliRunner().invoke(service_app, list(args), obj=obj)
    assert result.exit_code == 0, result.output


@pytest.fixture
def root_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checks: list[str] = []
    monkeypatch.setattr(
        "sing_box_cli.service.ensure_root", lambda: checks.append("root")
    )
    return checks


def test_restart_stops_and_starts_an_installed_service(root_checks: list[str]) -> None:
    service = Recorder()

    invoke(service, "restart")

    assert service.calls == ["sync_service", "stop", "start"]
    assert root_checks == ["root"]


def test_restart_creates_a_missing_service(root_checks: list[str]) -> None:
    service = Recorder(installed=False)

    invoke(service, "restart")

    assert service.calls == ["create_service", "stop", "start"]


def test_restart_clears_the_cache_while_stopped(root_checks: list[str]) -> None:
    service = Recorder()

    invoke(service, "restart", "--clear-cache")

    assert service.calls == ["sync_service", "stop", "config.clear_cache", "start"]


def test_restart_reloads_in_place_where_the_backend_can(root_checks: list[str]) -> None:
    service = ReloadingRecorder()

    invoke(service, "restart")

    assert service.calls == ["sync_service", "restart"]
    assert root_checks == []  # this backend does not need root


def test_reloading_backend_still_stops_to_clear_the_cache(
    root_checks: list[str],
) -> None:
    service = ReloadingRecorder()

    invoke(service, "restart", "--clear-cache")

    assert service.calls == ["sync_service", "stop", "config.clear_cache", "start"]


@pytest.mark.parametrize("command", ["enable", "disable", "stop", "status"])
def test_root_is_required_only_by_backends_that_need_it(
    root_checks: list[str], command: str
) -> None:
    class Quiet(ReloadingRecorder):
        def disable(self) -> None:
            self.calls.append("disable")

    invoke(Quiet(), command)
    assert root_checks == []

    class Privileged(Quiet):
        requires_root = True

    invoke(Privileged(), command)
    assert root_checks == ["root"]
