import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sing_box_bin import get_bin_path

from sing_box_cli.config.config import ConfigHandler
from sing_box_cli.service.manager import LinuxServiceManager

config_module = importlib.import_module("sing_box_cli.config.config")


@pytest.fixture
def config(tmp_path: Path) -> MagicMock:
    config = MagicMock(spec=ConfigHandler)
    config.config_dir = tmp_path / "Test User 代理配置"
    config.config_dir.mkdir()
    config.config_file = config.config_dir / "config.json"
    config.bin_path = get_bin_path()
    return config


@pytest.mark.parametrize("level", ["trace", "debug", "info", None])
def test_service_logs_warn_without_changing_user_config(
    config: MagicMock, level: str | None
) -> None:
    # Exercise sing-box's actual merge order, not an imitation of its JSON merge.
    original = {"outbounds": [{"type": "direct", "tag": "direct"}]}
    if level is not None:
        original["log"] = {"level": level, "output": "box.log", "timestamp": True}
    original_text = json.dumps(original)
    config.config_file.write_text(original_text, encoding="utf-8")
    config_module.prepare_service_config(config)
    args = config_module.service_run_args(config)
    merged = config.config_dir / "merged.json"
    subprocess.run(
        [args[0], "merge", str(merged), *args[2:]],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(merged.read_text(encoding="utf-8"))
    assert actual["log"]["level"] == "warn"
    assert actual["outbounds"] == original["outbounds"]
    if level is not None:
        assert actual["log"]["output"] == "box.log"
        assert actual["log"]["timestamp"] is True
    assert config.config_file.read_text(encoding="utf-8") == original_text
    assert "00-service-log.json" not in " ".join(config_module.run_args(config))


def test_linux_service_uses_warn_overlay(
    config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LinuxServiceManager(config)
    manager.service_file = tmp_path / "sing-box.service"
    monkeypatch.setattr(subprocess, "run", MagicMock())
    manager.create_service()
    assert "00-service-log.json" in manager.service_file.read_text()
    overlay = config.config_dir / "00-service-log.json"
    assert json.loads(overlay.read_text())["log"]["level"] == "warn"
