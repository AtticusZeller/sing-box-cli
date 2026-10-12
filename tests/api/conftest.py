from collections.abc import Iterator

import pytest
from fake_singbox import FakeSingBox


@pytest.fixture
def fake_singbox() -> Iterator[FakeSingBox]:
    server = FakeSingBox()
    server.start()
    try:
        yield server
    finally:
        server.stop()
