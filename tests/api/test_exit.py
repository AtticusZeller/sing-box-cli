import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.layout import BufferControl
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from sing_box_cli import api
from sing_box_cli.api.client import (
    ConnectionData,
    ConnectionInfo,
    ConnectionMetadata,
    GroupInfo,
    GroupsData,
    SingBoxAPIClient,
)
from sing_box_cli.api.connections import ConnectionsManager
from sing_box_cli.api.policy import PolicyGroupManager


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock(spec=SingBoxAPIClient)
    client.get_connections.return_value = ConnectionData(connections=[])
    client.get_groups.return_value = GroupsData(proxies=[])
    return client


@asynccontextmanager
async def running_manager(
    manager_type: type[ConnectionsManager] | type[PolicyGroupManager], client: MagicMock
) -> AsyncIterator[
    tuple[ConnectionsManager | PolicyGroupManager, PipeInput, asyncio.Task[str]]
]:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        manager = manager_type(client)
        ready = asyncio.Event()
        manager.app.before_render += lambda _: ready.set()

        async def run() -> str:
            try:
                await manager.run()
            except KeyboardInterrupt:
                return "interrupted"
            except EOFError:
                return "eof"
            return "exited"

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            yield manager, pipe, task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "manager_type,search_focus",
    [
        (ConnectionsManager, False),
        (ConnectionsManager, True),
        (PolicyGroupManager, False),
    ],
)
@pytest.mark.parametrize("key", ["\x03", "\x11", "sigint"])
def test_exit_keys_do_not_modify_connections(
    manager_type: type[ConnectionsManager] | type[PolicyGroupManager],
    search_focus: bool,
    key: str,
    client: MagicMock,
) -> None:
    async def scenario() -> None:
        async with running_manager(manager_type, client) as (manager, pipe, task):
            if search_focus:
                search = next(
                    control
                    for control in manager.layout.find_all_controls()
                    if isinstance(control, BufferControl)
                )
                manager.layout.focus(search)
            if key == "sigint":
                manager.app.key_processor.send_sigint()
            else:
                pipe.send_text(key)
            assert await asyncio.wait_for(asyncio.shield(task), timeout=1) == "exited"
            client.close_connection.assert_not_awaited()
            client.close_all_connections.assert_not_awaited()
            client.select_proxy.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "manager_type,key,method",
    [
        (ConnectionsManager, "\x18", "close_all_connections"),
        (ConnectionsManager, "\x04", "close_connection"),
        (PolicyGroupManager, "g", "test_group_delay"),
        (PolicyGroupManager, "\tt", "test_proxy_delay"),
        (PolicyGroupManager, "\t\r", "select_proxy"),
    ],
)
def test_exit_cancels_and_waits_for_pending_actions(
    manager_type: type[ConnectionsManager] | type[PolicyGroupManager],
    key: str,
    method: str,
    client: MagicMock,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        request_task: asyncio.Task[None] | None = None

        async def request(*_args: object) -> None:
            nonlocal request_task
            request_task = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        getattr(client, method).side_effect = request
        client.get_connections.return_value = ConnectionData(
            connections=[
                ConnectionInfo(
                    id="test-connection",
                    rule="MATCH",
                    start="2026-10-02T01:00:00Z",
                    chains=["DIRECT"],
                    metadata=ConnectionMetadata(
                        host="example.com",
                        destinationPort="443",
                        dnsMode="normal",
                        network="tcp",
                        sourceIP="127.0.0.1",
                        sourcePort="1234",
                        type="tcp",
                    ),
                )
            ]
        )
        group = GroupInfo(
            type="Selector", name="group", udp=True, now="DIRECT", all=["DIRECT"]
        )
        client.get_groups.return_value = GroupsData(proxies=[group])
        client.get_group.return_value = group
        try:
            async with running_manager(manager_type, client) as (_manager, pipe, task):
                pipe.send_text(key)
                await asyncio.wait_for(started.wait(), timeout=1)
                pipe.send_text("\x11")
                assert (
                    await asyncio.wait_for(asyncio.shield(task), timeout=1) == "exited"
                )
                assert cleaned.is_set()
                assert request_task is not None and request_task.done()
        finally:
            if request_task is not None:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["stats", "conns", "proxy", "logs"])
@pytest.mark.parametrize(
    "error_type", [None, KeyboardInterrupt, EOFError, RuntimeError]
)
def test_command_exit_status(
    command: str,
    error_type: type[BaseException] | None,
    client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "get_context_obj", lambda _ctx: MagicMock())
    monkeypatch.setattr(api, "create_client", lambda *_args: client)
    manager = MagicMock()
    error = error_type("stopped") if error_type else None
    manager.start = AsyncMock(side_effect=error)
    manager.run = AsyncMock(side_effect=error)
    for name in ["ResourceMonitor", "ConnectionsManager", "PolicyGroupManager"]:
        monkeypatch.setattr(api, name, lambda *_args: manager)
    monkeypatch.setattr(api, "get_logs", AsyncMock(side_effect=error))

    result = CliRunner().invoke(api.api, [command])
    if error_type is RuntimeError:
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "Exited." not in result.output
    else:
        assert result.exit_code == 0, result.output
        assert "Exited." in result.output
        assert "Traceback" not in result.output


@pytest.mark.parametrize("command", ["stats", "conns", "proxy", "logs"])
def test_interrupt_during_client_initialization_exits_cleanly(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "get_context_obj", lambda _ctx: MagicMock())
    monkeypatch.setattr(api, "create_client", MagicMock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(api.api, [command])
    assert result.exit_code == 0, result.output
    assert "Exited." in result.output


@pytest.mark.parametrize("manager_type", [ConnectionsManager, PolicyGroupManager])
def test_end_of_input_exits_without_delay(
    manager_type: type[ConnectionsManager] | type[PolicyGroupManager], client: MagicMock
) -> None:
    async def scenario() -> None:
        async with running_manager(manager_type, client) as (_manager, pipe, task):
            pipe.close()
            assert await asyncio.wait_for(asyncio.shield(task), timeout=1) == "eof"

    asyncio.run(scenario())


def test_close_selected_connection_is_safe_with_empty_list(client: MagicMock) -> None:
    async def scenario() -> None:
        async with running_manager(ConnectionsManager, client) as (
            _manager,
            pipe,
            task,
        ):
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()
            errors: list[dict[str, object]] = []
            loop.set_exception_handler(lambda _loop, context: errors.append(context))
            try:
                pipe.send_text("\x04\x11")
                assert (
                    await asyncio.wait_for(asyncio.shield(task), timeout=1) == "exited"
                )
                assert not errors
                client.close_connection.assert_not_awaited()
            finally:
                loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["close_connection", "close_all_connections"])
def test_connection_action_errors_are_reported_in_status(
    method: str, client: MagicMock
) -> None:
    async def scenario() -> None:
        getattr(client, method).side_effect = RuntimeError("request failed")
        async with running_manager(ConnectionsManager, client) as (manager, pipe, task):
            action = getattr(manager, method)
            args = ("test-connection",) if method == "close_connection" else ()
            await action(*args)
            assert "request failed" in manager.status_message
            assert not task.done()
            pipe.send_text("\x11")
            assert await asyncio.wait_for(asyncio.shield(task), timeout=1) == "exited"

    asyncio.run(scenario())
