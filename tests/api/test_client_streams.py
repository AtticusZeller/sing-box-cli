import asyncio
from contextlib import aclosing
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from sing_box_cli.api.client import LogEntry, MemoryData, SingBoxAPIClient, TrafficData


@pytest.mark.parametrize(
    "method,payload",
    [
        ("traffic_stream", TrafficData()),
        ("memory_stream", MemoryData()),
        ("log_stream", LogEntry(type="info", payload="message")),
    ],
)
def test_closing_stream_closes_underlying_request(
    method: str,
    payload: TrafficData | MemoryData | LogEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        closed = asyncio.Event()

        async def request():  # type: ignore[no-untyped-def]
            try:
                yield payload
            finally:
                closed.set()

        underlying = request()
        monkeypatch.setattr(SingBoxAPIClient, "_health_check", lambda _self: True)
        client = SingBoxAPIClient()
        monkeypatch.setattr(
            client, "_make_stream_request", MagicMock(return_value=underlying)
        )
        stream = getattr(client, method)()
        try:
            assert await anext(stream) == payload
            await stream.aclose()
            assert closed.is_set()
        finally:
            await stream.aclose()
            await underlying.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["traffic_stream", "memory_stream", "log_stream"])
def test_stream_request_bounds_connect_but_not_read(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead remote API must not hang reconnects; idle log streams must not time out."""

    async def scenario() -> None:
        timeouts: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: async_client(transport=transport, **kw)
        )
        monkeypatch.setattr(SingBoxAPIClient, "_health_check", lambda _self: True)
        client = SingBoxAPIClient(timeout=3.0)
        async with aclosing(getattr(client, method)()) as stream:
            async for _ in stream:
                pass
        assert len(timeouts) == 1
        assert timeouts[0]["connect"] == 3.0
        assert timeouts[0]["read"] is None

    asyncio.run(scenario())
