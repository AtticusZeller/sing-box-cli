import asyncio
from unittest.mock import MagicMock

import pytest

from sing_box_cli.api.logs import get_logs
from sing_box_cli.common import LogLevel


def test_log_stream_propagates_cancellation_after_cleanup() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        closed = asyncio.Event()

        async def stream(_level: str):  # type: ignore[no-untyped-def]
            try:
                started.set()
                await asyncio.Event().wait()
                yield None
            finally:
                closed.set()

        client = MagicMock()
        client.log_stream.side_effect = stream
        task = asyncio.create_task(get_logs(client, LogLevel.info))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_log_stream_does_not_treat_api_failure_as_success() -> None:
    async def stream(_level: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("connection lost")
        yield

    client = MagicMock()
    client.log_stream.side_effect = stream
    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(get_logs(client, LogLevel.info))


def test_log_stream_closes_on_rendering_error() -> None:
    async def scenario() -> None:
        closed = asyncio.Event()

        async def stream():  # type: ignore[no-untyped-def]
            try:
                yield None
            finally:
                closed.set()

        underlying = stream()
        client = MagicMock()
        client.log_stream.return_value = underlying
        try:
            with pytest.raises(AttributeError):
                await get_logs(client, LogLevel.info)
            assert closed.is_set()
        finally:
            await underlying.aclose()

    asyncio.run(scenario())
