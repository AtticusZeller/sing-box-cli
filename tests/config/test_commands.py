import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from sing_box_cli.config.config import ConfigHandler, ConfigModel, SingBoxConfig

OLD_URL = "https://sub.example.com/old.json"
NEW_URL = "https://sub.example.com/new.json"


@pytest.fixture
def client_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "client"
    monkeypatch.setattr(ConfigHandler, "is_windows", property(lambda _: True))
    monkeypatch.setattr(
        ConfigHandler, "bin_path", property(lambda _: tmp_path / "sing-box")
    )
    handler = importlib.import_module("sing_box_cli.config.config")
    monkeypatch.setattr(handler.typer, "get_app_dir", lambda *_, **__: str(directory))
    return directory


def seed_configs(directory: Path) -> None:
    directory.mkdir()
    (directory / "config.json").write_text(
        SingBoxConfig.model_validate({"log": {"level": "info"}}).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )
    (directory / "app_config.json").write_text(
        json.dumps({"subscription_url": OLD_URL, "token": "old-token"}),
        encoding="utf-8",
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_dry_run_only_previews(
    client_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    restart: bool,
) -> None:
    if existing:
        seed_configs(client_directory)
    before = {
        path.name: (path.read_bytes(), path.stat())
        for path in client_directory.glob("*")
    }
    main = importlib.import_module("sing_box_cli.main")
    cli = importlib.import_module("sing_box_cli.config")
    handler = importlib.import_module("sing_box_cli.config.config")
    request = MagicMock(
        return_value=httpx.Response(200, json={"log": {"level": "debug"}})
    )
    monkeypatch.setattr(handler, "request_get", request)
    # Exercise the Linux ownership branch as well as first-use initialization.
    monkeypatch.setattr(ConfigHandler, "is_windows", property(lambda _: False))
    original_init = ConfigHandler.__init__

    def init(config: ConfigHandler) -> None:
        monkeypatch.setattr(ConfigHandler, "is_windows", property(lambda _: True))
        original_init(config)
        monkeypatch.setattr(ConfigHandler, "is_windows", property(lambda _: False))

    monkeypatch.setattr(ConfigHandler, "__init__", init)
    forbidden = MagicMock(side_effect=AssertionError("Dry run must not mutate state"))
    monkeypatch.setattr(ConfigHandler, "init_directories", forbidden)
    monkeypatch.setattr(ConfigModel, "save", forbidden)
    monkeypatch.setattr(handler.shutil, "chown", forbidden)
    monkeypatch.setattr(cli, "create_service", forbidden)
    monkeypatch.setattr(cli, "ensure_root", forbidden)
    args = ["config", "update", NEW_URL, "--token", "new-token", "--dry-run"]
    if restart:
        args.append("--restart")
    result = CliRunner().invoke(main.app, args)
    assert result.exit_code == 0, result.output
    assert "--- old" in result.output
    assert "+++ new" in result.output
    assert '+    "level": "debug"' in result.output
    if existing:
        assert '-    "level": "info"' in result.output
    assert "Dry run:" in result.output
    request.assert_called_once_with(NEW_URL, "new-token")
    forbidden.assert_not_called()
    assert before == {
        path.name: (path.read_bytes(), path.stat())
        for path in client_directory.glob("*")
    }
    assert client_directory.exists() == existing


@pytest.mark.parametrize("response", [None, httpx.Response(200, text="invalid json")])
def test_failed_preview_does_not_initialize_or_restart(
    client_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response | None,
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    cli = importlib.import_module("sing_box_cli.config")
    handler = importlib.import_module("sing_box_cli.config.config")
    monkeypatch.setattr(handler, "request_get", lambda *_: response)
    forbidden = MagicMock(side_effect=AssertionError("Must not initialize or restart"))
    monkeypatch.setattr(ConfigHandler, "init_directories", forbidden)
    monkeypatch.setattr(cli, "create_service", forbidden)
    result = CliRunner().invoke(
        main.app, ["config", "update", NEW_URL, "--dry-run", "--restart"]
    )
    assert result.exit_code == 1, result.output
    forbidden.assert_not_called()
    assert not client_directory.exists()


def test_preview_unchanged_config_uses_saved_token(
    client_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_configs(client_directory)
    main = importlib.import_module("sing_box_cli.main")
    handler = importlib.import_module("sing_box_cli.config.config")
    request = MagicMock(
        return_value=httpx.Response(
            200, text=(client_directory / "config.json").read_text()
        )
    )
    monkeypatch.setattr(handler, "request_get", request)
    result = CliRunner().invoke(main.app, ["config", "update", NEW_URL, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Configuration is up to date" in result.output
    assert "Configuration differences" not in result.output
    request.assert_called_once_with(NEW_URL, "old-token")
    saved = json.loads((client_directory / "app_config.json").read_text())
    assert saved == {"subscription_url": OLD_URL, "token": "old-token"}


def test_normal_update_saves_config_and_subscription(
    client_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    handler = importlib.import_module("sing_box_cli.config.config")
    monkeypatch.setattr(
        handler,
        "request_get",
        lambda *_: httpx.Response(200, json={"log": {"level": "debug"}}),
    )
    result = CliRunner().invoke(
        main.app, ["config", "update", NEW_URL, "--token", "new-token"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads((client_directory / "config.json").read_text())["log"] == {
        "level": "debug"
    }
    assert json.loads((client_directory / "app_config.json").read_text()) == {
        "subscription_url": NEW_URL,
        "token": "new-token",
    }


@pytest.mark.parametrize(
    "arguments,subscription",
    [
        (["get"], False),
        (["get", "--subscription"], True),
        (["show"], False),
        (["show-sub"], True),
    ],
)
def test_get_and_compatibility_aliases(
    client_directory: Path, arguments: list[str], subscription: bool
) -> None:
    seed_configs(client_directory)
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["config", *arguments])
    assert result.exit_code == 0, result.output
    if subscription:
        assert OLD_URL in result.output
        assert '"level"' not in result.output
    else:
        assert '"level": "info"' in result.output
        assert OLD_URL not in result.output


def test_get_reports_missing_subscription(client_directory: Path) -> None:
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["config", "get", "--subscription"])
    assert result.exit_code == 0, result.output
    assert "No subscription URL found" in result.output
    assert not client_directory.exists()


def test_config_help_promotes_get() -> None:
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "get" in result.output
    assert "show-sub" not in result.output
    assert "Compatibility alias" not in result.output


@pytest.mark.parametrize("dry_run", [False, True])
def test_update_extracts_url_token_before_saved_token_and_logging(
    client_directory: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    seed_configs(client_directory)
    main = importlib.import_module("sing_box_cli.main")
    handler = importlib.import_module("sing_box_cli.config.config")
    request = MagicMock(
        return_value=httpx.Response(200, json={"log": {"level": "debug"}})
    )
    monkeypatch.setattr(handler, "request_get", request)
    args = ["config", "update", NEW_URL + "?token=url-subscription-token"]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(main.app, args)
    assert result.exit_code == 0, result.output
    assert "url-subscription-token" not in result.output
    request.assert_called_once_with(NEW_URL, "url-subscription-token")
    saved = json.loads((client_directory / "app_config.json").read_text())
    assert saved == {
        "subscription_url": OLD_URL if dry_run else NEW_URL,
        "token": "old-token" if dry_run else "url-subscription-token",
    }
