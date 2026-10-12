import asyncio
from collections.abc import Callable
from io import StringIO
from unittest.mock import MagicMock

import httpx
import pytest
from fake_singbox import FakeSingBox
from rich.console import Console

from sing_box_cli.api import monitor
from sing_box_cli.api.client import MemoryData, SingBoxAPIClient, TrafficData


async def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("sample_count", [0, 1, 3])
def test_traffic_graph_renders_without_clf_alias(
    monkeypatch: pytest.MonkeyPatch, sample_count: int
) -> None:
    monkeypatch.delattr(monitor.plt, "clf", raising=False)
    graph = monitor.TrafficGraph()
    for _ in range(sample_count):
        graph.update_from_traffic_data(TrafficData(up=1024, down=2048))

    output = StringIO()
    console = Console(file=output, width=80, height=20, color_system=None)
    for _ in range(2):
        console.print(graph)

    rendered = output.getvalue()
    assert "Error rendering graph" not in rendered
    if sample_count == 0:
        assert "Collecting data..." in rendered
    else:
        assert "Upload" in rendered
        assert "Download" in rendered


def test_monitor_cleans_up_tasks_if_live_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        visualizer = MagicMock()
        resource_monitor = monitor.ResourceMonitor(MagicMock(), visualizer)
        tasks: list[asyncio.Task[None]] = []
        create_task = asyncio.create_task

        def track_task(coro):  # type: ignore[no-untyped-def]
            task = create_task(coro)
            tasks.append(task)
            return task

        live = MagicMock()
        live.__enter__.side_effect = RuntimeError("terminal unavailable")
        monkeypatch.setattr(monitor, "Live", MagicMock(return_value=live))
        monkeypatch.setattr(monitor.asyncio, "create_task", track_task)
        try:
            with pytest.raises(RuntimeError, match="terminal unavailable"):
                await resource_monitor.refresh_display()
            assert not resource_monitor.running
            assert len(tasks) == 2
            assert all(task.done() for task in tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("yield_first", [False, True])
def test_monitor_propagates_cancellation_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, yield_first: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        closed: list[str] = []

        async def stream(name: str):  # type: ignore[no-untyped-def]
            started.set()
            try:
                if yield_first:
                    yield TrafficData() if name == "traffic" else MemoryData()
                await asyncio.Event().wait()
            finally:
                closed.append(name)

        client = MagicMock()
        traffic_stream = stream("traffic")
        memory_stream = stream("memory")
        client.traffic_stream.return_value = traffic_stream
        client.memory_stream.return_value = memory_stream
        visualizer = MagicMock(refresh_rate=0.25)
        resource_monitor = monitor.ResourceMonitor(client, visualizer)
        monkeypatch.setattr(monitor, "Live", MagicMock())
        task = asyncio.create_task(resource_monitor.start())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not resource_monitor.running
            assert sorted(closed) == ["memory", "traffic"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await traffic_stream.aclose()
            await memory_stream.aclose()

    asyncio.run(scenario())


def test_monitor_reconnects_after_singbox_restart(
    fake_singbox: FakeSingBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        visualizer = MagicMock(refresh_rate=0.05)
        client = SingBoxAPIClient(fake_singbox.base_url)
        resource_monitor = monitor.ResourceMonitor(client, visualizer)
        resource_monitor.task_interval = 0.01
        resource_monitor.reconnect_interval = 0.05
        monkeypatch.setattr(monitor, "Live", MagicMock())

        def footer_error() -> str | None:
            call = visualizer.create_resources_layout.call_args
            return call.args[2] if call and len(call.args) > 2 else None

        task = asyncio.create_task(resource_monitor.start())
        try:
            await wait_until(
                lambda: resource_monitor.current_traffic
                == TrafficData(up=100, down=200)
                and resource_monitor.current_memory.inuse == 1024
            )
            assert footer_error() is None

            fake_singbox.stop()
            # stale numbers must not linger while sing-box is down
            await wait_until(
                lambda: resource_monitor.current_traffic == TrafficData()
                and resource_monitor.current_memory == MemoryData()
            )
            await wait_until(lambda: "reconnecting" in (footer_error() or ""))

            fake_singbox.traffic = {"up": 300, "down": 400}
            fake_singbox.start()
            await wait_until(lambda: resource_monitor.current_traffic.up == 300)
            await wait_until(lambda: footer_error() is None)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["eof", "error"])
def test_monitor_reopens_stream_after_it_ends(
    ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        opened = 0

        async def traffic_stream():  # type: ignore[no-untyped-def]
            nonlocal opened
            opened += 1
            yield TrafficData(up=opened)
            if ending == "error":
                raise httpx.ReadError("connection reset")

        async def memory_stream():  # type: ignore[no-untyped-def]
            yield MemoryData(inuse=1)
            await asyncio.Event().wait()

        client = MagicMock()
        client.traffic_stream.side_effect = traffic_stream
        client.memory_stream.side_effect = memory_stream
        resource_monitor = monitor.ResourceMonitor(client, MagicMock(refresh_rate=0.05))
        resource_monitor.task_interval = 0.01
        resource_monitor.reconnect_interval = 0.01
        monkeypatch.setattr(monitor, "Live", MagicMock())
        task = asyncio.create_task(resource_monitor.start())
        try:
            await wait_until(lambda: resource_monitor.current_traffic.up >= 3)
            assert resource_monitor.current_memory.inuse == 1
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_resources_layout_shows_api_error() -> None:
    visualizer = monitor.ResourceVisualizer()
    output = StringIO()
    console = Console(file=output, width=100, height=30, color_system=None)
    console.print(
        visualizer.create_resources_layout(
            TrafficData(), MemoryData(), "sing-box API unreachable, reconnecting..."
        )
    )
    assert "sing-box API unreachable, reconnecting..." in output.getvalue()
