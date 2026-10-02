import asyncio
from unittest.mock import MagicMock

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
