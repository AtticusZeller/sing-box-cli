from io import StringIO

import pytest
from rich.console import Console

from sing_box_cli.api import monitor
from sing_box_cli.api.client import TrafficData


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
