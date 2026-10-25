"""ConfigHandler on the profile store, with and without the sing-box desktop app."""

import importlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from sing_box_cli.service import desktop

# `sing_box_cli.config.config` is shadowed by the Typer app of the same name
config_module = importlib.import_module("sing_box_cli.config.config")

OLD = '{"experimental":{"clash_api":{"external_controller":"127.0.0.1:9090"}}}'
# deliberately not what pydantic would dump, the desktop app compares raw bytes
NEW = '{\n"outbounds" : [],\n  "experimental":{"clash_api":{"external_controller":"127.0.0.1:9191"}}}\n'
APP_URL = "https://sub.example.com/client.json?token=s3cret"
APP_URL_BARE = "https://sub.example.com/client.json"

SettingsDb = Callable[..., Path]
Serve = Callable[[str | None], list[tuple[str, str]]]

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="the sing-box desktop app backend is Linux only"
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="needs links")


@pytest.fixture(params=[False, True], ids=["linux-config", "windows-config"])
def config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    directory = tmp_path / "sing-box"
    monkeypatch.setattr(
        config_module.ConfigHandler, "is_windows", property(lambda self: request.param)
    )
    monkeypatch.setenv("USER", "tester")
    monkeypatch.delenv("SUDO_USER", raising=False)
    # where the config directory comes from, on Linux and on Windows
    monkeypatch.setattr(Path, "expanduser", lambda self: directory)
    monkeypatch.setattr(
        config_module.typer, "get_app_dir", lambda *_, **__: str(directory)
    )
    return directory


@pytest.fixture
def installed_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile ownership is independent of the platform's daemon transport."""
    monkeypatch.setattr(desktop, "desktop_app_installed", lambda: True)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Serve:
    """Serve ``text`` (None: download fails); returns the (url, token) requests."""

    def serve(text: str | None) -> list[tuple[str, str]]:
        requested: list[tuple[str, str]] = []

        def fake(url: str, token: str) -> Any:
            requested.append((url, token))
            return None if text is None else SimpleNamespace(text=text)

        monkeypatch.setattr(config_module, "request_get", fake)
        return requested

    return serve


def _profiles(db_path: Path) -> list[tuple[str, str, str | None, int | None]]:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(
            "SELECT id, type, remote_url, last_updated FROM profiles ORDER BY item_order"
        ).fetchall()
    finally:
        db.close()


def _snapshot(directory: Path) -> dict[str, bytes | str]:
    return {
        str(path.relative_to(directory)): (
            "link" if path.is_symlink() else path.read_bytes()
        )
        for path in directory.rglob("*")
        if path.is_file() or path.is_symlink()
    }


# ============================================================ desktop app installed
@pytest.fixture
def app(
    config_dir: Path, make_settings_db: SettingsDb, installed_app: None
) -> tuple[Path, Path]:
    """A desktop app with one selected remote profile: (profile file, settings.db)."""
    (config_dir / "profiles").mkdir(parents=True)
    profile = config_dir / "profiles" / "aaa.json"
    profile.write_text(OLD)
    profile.chmod(0o600)
    db = make_settings_db(config_dir, selected="aaa", profiles={"aaa": APP_URL})
    return profile, db


def test_profile_and_subscription_come_from_the_app(
    config_dir: Path, app: tuple[Path, Path], served: Serve
) -> None:
    profile, db_path = app
    other = config_dir / "profiles" / "bbb.json"  # newer, but not selected
    other.write_text("{}")
    requested = served(NEW)

    handler = config_module.get_config()
    assert handler.external_config
    assert handler.config_file == profile
    # the token stays out of what is shown, it is sent as a bearer token
    assert handler.sub_url == APP_URL_BARE
    assert handler.token_content == "s3cret"
    assert handler.api_base_url == "http://127.0.0.1:9090"

    assert handler.update_config()  # no URL: the app's own one

    assert requested == [(APP_URL_BARE, "s3cret")]
    assert profile.read_bytes() == NEW.encode("utf-8")  # no CRLF conversion on Windows
    if os.name != "nt":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert other.read_text() == "{}"
    assert not list((config_dir / "profiles").glob(".*"))
    [(_, _, url, updated)] = _profiles(db_path)
    assert url == APP_URL  # the app's URL is left exactly as it was
    assert updated is not None  # and the app lists the refresh


@pytest.mark.usefixtures("app")
def test_sbc_creates_nothing_next_to_the_app(config_dir: Path, served: Serve) -> None:
    served(NEW)
    handler = config_module.get_config()
    assert handler.update_config("https://other.example.com/b.json")

    assert handler.sub_url == "https://other.example.com/b.json"
    assert handler.legacy_config is None
    assert not (config_dir / "config.json").exists()
    assert not (config_dir / "app_config.json").exists()


def test_new_subscription_is_written_to_the_app(
    app: tuple[Path, Path], served: Serve
) -> None:
    _, db_path = app
    other = "https://other.example.com/b.json?token=xyz&flag=1"
    requested = served(NEW)

    assert config_module.get_config().update_config(other)

    assert requested == [("https://other.example.com/b.json?flag=1", "xyz")]
    assert _profiles(db_path)[0][2] == other  # stored as given
    again = config_module.ConfigHandler()  # a new run sees it too
    assert (again.sub_url, again.token_content) == (
        "https://other.example.com/b.json?flag=1",
        "xyz",
    )


def test_separate_token_is_stored_inside_the_url(
    app: tuple[Path, Path], served: Serve
) -> None:
    _, db_path = app
    served(NEW)

    assert config_module.get_config().update_config(
        "https://other.example.com/b.json", "t-1"
    )

    assert _profiles(db_path)[0][2] == "https://other.example.com/b.json?token=t-1"


def test_new_url_without_token_keeps_the_saved_token(
    app: tuple[Path, Path], served: Serve
) -> None:
    _, db_path = app
    requested = served(NEW)

    assert config_module.get_config().update_config("https://other.example.com/b.json")

    assert requested == [("https://other.example.com/b.json", "s3cret")]
    assert _profiles(db_path)[0][2] == "https://other.example.com/b.json?token=s3cret"


def test_subscription_not_saved_when_download_fails(
    app: tuple[Path, Path], served: Serve
) -> None:
    profile, db_path = app
    served(None)

    assert not config_module.get_config().update_config("https://typo.example.com/")
    assert _profiles(db_path) == [("aaa", "remote", APP_URL, None)]
    assert profile.read_text() == OLD


def test_profile_untouched_on_invalid_download(
    app: tuple[Path, Path], served: Serve
) -> None:
    profile, db_path = app
    served("<html>login</html>")

    assert not config_module.get_config().update_config()
    assert profile.read_text() == OLD
    assert _profiles(db_path) == [("aaa", "remote", APP_URL, None)]


def test_formatting_alone_is_not_a_change(
    app: tuple[Path, Path], served: Serve, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, _ = app
    served(json.dumps(json.loads(OLD), indent=4) + "\n")

    assert config_module.get_config().update_config()

    assert "Configuration is up to date" in capsys.readouterr().out
    assert profile.read_text() == OLD


def test_dry_run_changes_nothing_in_the_app(
    config_dir: Path,
    app: tuple[Path, Path],
    served: Serve,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, db_path = app
    served(NEW)
    before = _snapshot(config_dir)

    handler = config_module.get_config(initialize=False)
    assert handler.update_config("https://other.example.com/b.json", dry_run=True)

    assert "Configuration differences" in capsys.readouterr().out
    assert _snapshot(config_dir) == before
    assert _profiles(db_path) == [("aaa", "remote", APP_URL, None)]


@pytest.mark.usefixtures("installed_app")
def test_local_profile_of_the_app_has_no_subscription(
    config_dir: Path,
    make_settings_db: SettingsDb,
    served: Serve,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (config_dir / "profiles").mkdir(parents=True)
    profile = config_dir / "profiles" / "loc.json"
    profile.write_text(OLD)
    make_settings_db(config_dir, selected="loc", profiles={"loc": None})
    requested = served(NEW)
    handler = config_module.get_config()

    assert handler.sub_url == ""
    assert not handler.update_config()
    assert "No subscription URL found" in capsys.readouterr().out
    assert not handler.update_config("https://example.com/sub")
    assert "local profile" in capsys.readouterr().out
    assert requested == []
    assert profile.read_text() == OLD


@pytest.mark.usefixtures("installed_app")
def test_unreadable_app_settings_are_an_error_not_a_guess(
    config_dir: Path, served: Serve, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "profiles" / "p.json").write_text(OLD)  # a guess would find it
    (config_dir / "config.json").write_text(OLD)
    (config_dir / "app_config.json").write_text(
        json.dumps({"subscription_url": "https://sbc.example.com/x"})
    )
    requested = served(NEW)
    before = _snapshot(config_dir)

    handler = config_module.get_config()
    assert handler.external_config  # still the desktop app, never the systemd path
    capsys.readouterr()

    for action in (
        handler.update_config,
        lambda: handler.sub_url,
        lambda: handler.api_base_url,
        lambda: handler.config_file_content,
    ):
        with pytest.raises(typer.Exit):
            action()
        assert "settings" in capsys.readouterr().out

    assert requested == []
    # nothing imported, nothing created: the store is the app's to create
    assert _snapshot(config_dir) == before


@pytest.mark.usefixtures("installed_app")
def test_no_selected_profile_is_an_error(
    config_dir: Path, make_settings_db: SettingsDb, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "profiles" / "p.json").write_text(OLD)
    make_settings_db(config_dir, profiles={"p": APP_URL})  # nothing selected

    with pytest.raises(typer.Exit):
        config_module.get_config().update_config()
    assert "No profile is selected" in capsys.readouterr().out


@linux_only
def test_installed_app_wins_over_files_on_disk(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The app is installed (its unit exists) although its daemon is not running."""
    unit = tmp_path / "sing-box-daemon.service"
    unit.write_text("")
    monkeypatch.setattr(desktop, "DAEMON_UNIT", unit)
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(OLD)  # sbc's own file from before

    handler = config_module.get_config()

    assert handler.external_config
    assert handler.config_file != config_dir / "config.json"
    assert not (config_dir / "settings.db").exists()


# ================================================================ no desktop app
def test_reading_creates_nothing(config_dir: Path) -> None:
    handler = config_module.get_config(initialize=False)

    assert not handler.external_config
    assert handler.config_file_content == "{}"
    assert handler.sub_url == ""
    assert not config_dir.exists()


def test_first_use_creates_the_store(config_dir: Path) -> None:
    handler = config_module.get_config()

    [(profile_id, kind, url, _)] = _profiles(config_dir / "settings.db")
    assert (kind, url) == ("local", None)
    assert handler.config_file == config_dir / "profiles" / f"{profile_id}.json"
    assert handler.config_file_content == "{}"
    assert not handler.external_config
    assert handler.sub_url == ""
    assert handler.legacy_config is None
    assert config_module.get_config().config_file == handler.config_file  # once


def test_subscription_of_a_local_profile_makes_it_remote(
    config_dir: Path, served: Serve
) -> None:
    requested = served(NEW)

    assert config_module.get_config().update_config("https://example.com/sub?token=t")

    db_path = config_dir / "settings.db"
    [(profile_id, kind, url, updated)] = _profiles(db_path)
    assert (kind, url) == ("remote", "https://example.com/sub?token=t")
    assert updated is not None
    assert requested == [("https://example.com/sub", "t")]
    assert (config_dir / "profiles" / f"{profile_id}.json").read_text() == NEW
    # and the next run, and `config update` without a URL, use it
    again = config_module.get_config()
    assert (again.sub_url, again.token_content) == ("https://example.com/sub", "t")
    assert again.update_config()
    assert requested[-1] == ("https://example.com/sub", "t")


def test_older_files_are_shown_before_and_imported_on_first_use(
    config_dir: Path,
) -> None:
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(OLD)
    (config_dir / "app_config.json").write_text(
        json.dumps({"subscription_url": "https://h.example/p.json", "token": "ab-c"})
    )
    before = _snapshot(config_dir)

    # reading shows the very same values without importing anything yet
    peek = config_module.get_config(initialize=False)
    assert (peek.sub_url, peek.token_content) == ("https://h.example/p.json", "ab-c")
    assert peek.config_file_content == OLD
    assert peek.legacy_config is None  # nothing to follow up before the import
    assert _snapshot(config_dir) == before

    handler = config_module.get_config()

    [(profile_id, kind, url, _)] = _profiles(config_dir / "settings.db")
    assert (kind, url) == ("remote", "https://h.example/p.json?token=ab-c")
    assert handler.config_file == config_dir / "profiles" / f"{profile_id}.json"
    assert handler.config_file_content == OLD  # the configuration is kept as it was
    assert (handler.sub_url, handler.token_content) == (
        "https://h.example/p.json",
        "ab-c",
    )


@posix_only
def test_update_goes_through_the_old_config_path_too(
    config_dir: Path, served: Serve
) -> None:
    """A service created before keeps running config.json, which is the profile."""
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(OLD)
    served(NEW)

    handler = config_module.get_config()
    assert handler.update_config("https://example.com/sub")

    assert handler.legacy_config is None  # it is a link to the profile now
    assert (config_dir / "config.json").read_text() == NEW


def test_config_that_cannot_be_linked_is_left_for_the_service_sync(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the privilege to link (Windows) the old file stays where it is."""
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(OLD)

    def refuse(self: Path, target: Path) -> None:
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refuse)

    handler = config_module.get_config()

    assert handler.config_file.parent == config_dir / "profiles"
    assert handler.config_file_content == OLD
    assert handler.legacy_config == config_dir / "config.json"


def test_dry_run_changes_nothing_in_our_store(config_dir: Path, served: Serve) -> None:
    served(OLD)
    assert config_module.get_config().update_config("https://example.com/sub?token=t")
    before = _snapshot(config_dir)
    served(NEW)

    handler = config_module.get_config(initialize=False)
    assert handler.update_config("https://example.com/other", dry_run=True)

    assert _snapshot(config_dir) == before


def test_run_command_uses_the_profile(config_dir: Path) -> None:
    handler = config_module.get_config()

    assert handler.config_file.parent == config_dir / "profiles"
    assert f"-c {handler.config_file} " in config_module.run_cmd(handler)
    assert str(handler.config_file) in config_module.run_args(handler)


@pytest.mark.parametrize("desktop", [False, True])
def test_constructor_is_silent(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    desktop: bool,
) -> None:
    monkeypatch.setattr(
        config_module.ConfigHandler, "_detect_desktop_app", lambda self: desktop
    )
    config_module.ConfigHandler()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
