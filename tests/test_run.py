import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from sing_box_cli.config.config import ConfigHandler
from sing_box_cli.service import SharedContext


def test_run_preserves_paths_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    main = importlib.import_module("sing_box_cli.main")
    config = MagicMock(spec=ConfigHandler)
    config.bin_path = Path("C:/Program Files/sing-box/sing-box.exe")
    config.config_dir = Path("C:/Users/Test User/代理配置")
    config.config_file = config.config_dir / "config.json"
    config.config_file_content = '{"log": {}}'
    monkeypatch.setattr(main, "get_config", lambda: config)
    monkeypatch.setattr(main, "create_service", lambda _config: MagicMock())
    monkeypatch.setattr(main, "ensure_root", lambda: None)
    run = MagicMock()
    monkeypatch.setattr(main.subprocess, "run", run)

    result = CliRunner().invoke(main.app, ["run"])
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        [
            str(config.bin_path),
            "run",
            "-c",
            str(config.config_file),
            "-D",
            str(config.config_dir),
        ]
    )


def test_enable_does_not_report_success_on_start_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = importlib.import_module("sing_box_cli.service")
    manager = MagicMock()
    manager.start.side_effect = RuntimeError("start failed")
    context = MagicMock(obj=SharedContext(config=MagicMock(), service=manager))
    monkeypatch.setattr(service, "ensure_root", lambda: None)
    with pytest.raises(RuntimeError, match="start failed"):
        service.service_enable(context)
    assert "Service started" not in capsys.readouterr().out
