"""The profile store: settings.db + profiles/, the layout of the sing-box desktop app."""

import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sing_box_cli.config.store import (
    Profile,
    SettingsError,
    active_profile,
    init_store,
    invoking_user,
    legacy_profile,
    update_profile_record,
)
from sing_box_cli.config.utils import atomic_write_text, redact_url, with_token

OLD = '{"log": {"level": "info"}}'
URL = "https://sub.example.com/client.json?token=s3cret"

SettingsDb = Callable[..., Path]

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="sudo is Linux only")
posix_only = pytest.mark.skipif(os.name == "nt", reason="needs links and file modes")


def _rows(directory: Path) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(directory / "settings.db")
    try:
        return db.execute(
            "SELECT id, type, remote_url, auto_update, last_updated "
            "FROM profiles ORDER BY item_order"
        ).fetchall()
    finally:
        db.close()


def _selected(directory: Path) -> str:
    db = sqlite3.connect(directory / "settings.db")
    try:
        row = db.execute(
            "SELECT data FROM preferences WHERE name = 'selected_profile_id'"
        ).fetchone()
    finally:
        db.close()
    return str(json.loads(bytes(row[0])))


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _seed_older_version(directory: Path, *, token: str = "ab-c") -> None:
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(OLD, encoding="utf-8")
    (directory / "app_config.json").write_text(
        json.dumps({"subscription_url": "https://h.example/p.json", "token": token}),
        encoding="utf-8",
    )


# ================================================================ our own store
def test_first_use_creates_an_empty_local_profile(tmp_path: Path) -> None:
    directory = tmp_path / "sing-box"

    profile = init_store(directory)

    [(profile_id, kind, url, auto_update, updated)] = _rows(directory)
    assert (kind, url, auto_update, updated) == ("local", None, 0, None)
    assert _selected(directory) == profile_id == profile.id
    assert profile == Profile(
        path=directory / "profiles" / f"{profile_id}.json", remote_url=None
    )
    assert profile.path.read_text(encoding="utf-8") == "{}"
    assert _names(directory) == ["profiles", "settings.db"]


def test_store_is_opened_not_recreated(tmp_path: Path) -> None:
    first = init_store(tmp_path)
    second = init_store(tmp_path)

    assert first == second
    assert len(_rows(tmp_path)) == 1


def test_store_works_in_a_directory_with_spaces_and_other_characters(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "Test User #1" / "代理配置"

    profile = init_store(directory)
    update_profile_record(directory, profile.id, remote_url=URL, make_remote=True)

    assert active_profile(directory).remote_url == URL


def test_the_store_has_the_desktop_apps_schema(tmp_path: Path) -> None:
    """So the app, if installed later, takes the database over as it is."""
    init_store(tmp_path)

    db = sqlite3.connect(tmp_path / "settings.db")
    try:
        tables = {
            name: [col[1] for col in db.execute(f"PRAGMA table_info({name})")]
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        db.close()
    assert tables == {
        "profiles": [
            "id",
            "name",
            "type",
            "remote_url",
            "auto_update",
            "auto_update_interval_minutes",
            "last_updated",
            "item_order",
        ],
        "preferences": ["name", "data"],
        "remote_servers": ["id", "name", "url", "secret", "item_order"],
    }


def test_older_files_are_imported(tmp_path: Path) -> None:
    directory = tmp_path / "sing-box"
    _seed_older_version(directory)

    profile = init_store(directory)

    assert profile.path.read_text(encoding="utf-8") == OLD
    # the separate token joins the URL, where the desktop app keeps it
    assert profile.remote_url == "https://h.example/p.json?token=ab-c"
    [(_, kind, url, _, _)] = _rows(directory)
    assert (kind, url) == ("remote", profile.remote_url)
    assert not (directory / "app_config.json").exists()
    assert (directory / "app_config.json.migrated").exists()

    assert init_store(directory) == profile  # once
    assert len(_rows(directory)) == 1


def test_older_files_without_a_subscription_become_a_local_profile(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "sing-box"
    directory.mkdir()
    (directory / "config.json").write_text(OLD, encoding="utf-8")
    (directory / "app_config.json").write_text("not json", encoding="utf-8")

    profile = init_store(directory)

    assert profile.remote_url is None
    assert profile.path.read_text(encoding="utf-8") == OLD
    assert _rows(directory)[0][1] == "local"


@posix_only
def test_old_config_path_becomes_a_link_to_the_profile(tmp_path: Path) -> None:
    """A service created before keeps running config.json, which is the profile."""
    directory = tmp_path / "sing-box"
    _seed_older_version(directory)

    profile = init_store(directory)

    assert (directory / "config.json").is_symlink()
    assert (directory / "config.json").resolve() == profile.path.resolve()
    assert (directory / "config.json.migrated").read_text(encoding="utf-8") == OLD


@posix_only
def test_config_linked_elsewhere_by_the_user_is_imported(tmp_path: Path) -> None:
    """config.json may already be a link, to a file the user keeps elsewhere."""
    directory = tmp_path / "sing-box"
    directory.mkdir()
    elsewhere = tmp_path / "dotfiles" / "sing-box.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(OLD, encoding="utf-8")
    (directory / "config.json").symlink_to(elsewhere)

    profile = init_store(directory)

    assert profile.path.read_text(encoding="utf-8") == OLD
    assert (directory / "config.json").resolve() == profile.path.resolve()
    # their link is put aside, not followed or overwritten
    assert (directory / "config.json.migrated").resolve() == elsewhere.resolve()
    assert elsewhere.read_text(encoding="utf-8") == OLD


def test_config_that_cannot_be_linked_stays_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the privilege to link (Windows) the old file is left where it is."""
    directory = tmp_path / "sing-box"
    _seed_older_version(directory)

    def refuse(self: Path, target: Path) -> None:
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refuse)

    profile = init_store(directory)

    assert profile.path.read_text(encoding="utf-8") == OLD
    config = directory / "config.json"
    assert config.is_file() and not config.is_symlink()
    assert config.read_text(encoding="utf-8") == OLD
    assert not (directory / "config.json.migrated").exists()


def test_selection_is_repaired_from_the_profiles_that_exist(
    tmp_path: Path, make_settings_db: SettingsDb
) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "aaa.json").write_text(OLD, encoding="utf-8")
    make_settings_db(tmp_path, selected="gone", profiles={"gone": URL, "aaa": None})

    profile = init_store(tmp_path)

    assert profile.id == "aaa"
    assert _selected(tmp_path) == "aaa"
    assert len(_rows(tmp_path)) == 2  # nothing new was made up


# ============================================================ read-only views
def test_legacy_profile_is_a_view_that_writes_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "nothing-yet"
    assert legacy_profile(empty) == Profile(path=empty / "config.json", remote_url=None)
    assert not empty.exists()

    directory = tmp_path / "sing-box"
    _seed_older_version(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    profile = legacy_profile(directory)

    assert profile == Profile(
        path=directory / "config.json", remote_url="https://h.example/p.json?token=ab-c"
    )
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_active_profile_is_the_one_selected_in_the_app(
    tmp_path: Path, make_settings_db: SettingsDb
) -> None:
    (tmp_path / "profiles").mkdir()
    for name in ("aaa", "loc"):
        (tmp_path / "profiles" / f"{name}.json").write_text(OLD, encoding="utf-8")
    make_settings_db(tmp_path, selected="aaa", profiles={"aaa": URL, "loc": None})

    assert active_profile(tmp_path) == Profile(
        path=tmp_path / "profiles" / "aaa.json", remote_url=URL
    )

    make_settings_db(tmp_path, selected="loc")
    assert active_profile(tmp_path).remote_url is None  # a local profile


def test_active_profile_never_guesses(
    tmp_path: Path, make_settings_db: SettingsDb
) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "aaa.json").write_text(OLD, encoding="utf-8")

    with pytest.raises(SettingsError, match="do not exist"):
        active_profile(tmp_path)

    make_settings_db(tmp_path, profiles={"aaa": URL})
    with pytest.raises(SettingsError, match="No profile is selected"):
        active_profile(tmp_path)

    make_settings_db(tmp_path, selected="../aaa")  # not a profile id
    with pytest.raises(SettingsError, match="No profile is selected"):
        active_profile(tmp_path)

    make_settings_db(tmp_path, selected="unknown")
    with pytest.raises(SettingsError, match="is not in the settings"):
        active_profile(tmp_path)

    make_settings_db(tmp_path, selected="bbb", profiles={"bbb": URL})
    with pytest.raises(SettingsError, match="bbb.json not found"):
        active_profile(tmp_path)

    assert not list(tmp_path.glob("settings.db-*"))  # and leaves nothing behind


# ==================================================================== updates
def test_refresh_and_subscription_are_recorded(
    tmp_path: Path, make_settings_db: SettingsDb
) -> None:
    make_settings_db(tmp_path, profiles={"aaa": URL, "loc": None})

    update_profile_record(tmp_path, "aaa")
    [(_, _, url, _, updated), _] = _rows(tmp_path)
    assert url == URL
    assert updated is not None

    update_profile_record(tmp_path, "aaa", remote_url="https://new.example/x")
    assert _rows(tmp_path)[0][2] == "https://new.example/x"


def test_local_profile_becomes_remote_only_when_asked(
    tmp_path: Path, make_settings_db: SettingsDb
) -> None:
    make_settings_db(tmp_path, profiles={"loc": None})

    # a local profile of the desktop app is not ours to change
    with pytest.raises(SettingsError, match="not a remote profile"):
        update_profile_record(tmp_path, "loc", remote_url=URL)
    assert _rows(tmp_path)[0][1:3] == ("local", None)

    update_profile_record(tmp_path, "loc", remote_url=URL, make_remote=True)
    assert _rows(tmp_path)[0][1:3] == ("remote", URL)


def test_updating_a_missing_store_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="Cannot update the settings"):
        update_profile_record(tmp_path, "aaa")
    assert not (tmp_path / "settings.db").exists()


# ======================================================================= sudo
@linux_only
def test_sudo_acts_as_the_invoking_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "seteuid", lambda value: calls.append(("euid", value)))
    monkeypatch.setattr(os, "setegid", lambda value: calls.append(("egid", value)))
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1001")

    with invoking_user():
        calls.append("inside")

    assert calls == [("egid", 1001), ("euid", 1000), "inside", ("euid", 0), ("egid", 0)]


@linux_only
def test_without_sudo_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(os, "seteuid", lambda value: calls.append(value))
    monkeypatch.setattr(os, "geteuid", lambda: 0)  # root itself, not through sudo
    monkeypatch.delenv("SUDO_UID", raising=False)

    with invoking_user():
        pass

    assert calls == []


# ==================================================================== helpers
def test_with_token() -> None:
    assert with_token("https://h.example/p", "t") == "https://h.example/p?token=t"
    assert (
        with_token("https://h.example/p?a=1", "t") == "https://h.example/p?a=1&token=t"
    )
    assert (
        with_token("https://h.example/p?token=x", "t") == "https://h.example/p?token=x"
    )


def test_redact_url() -> None:
    assert redact_url("https://h.example/p.json?x=1") == "https://h.example/p.json?x=1"
    assert redact_url("subscription URL") == "subscription URL"
    assert (
        redact_url("https://u:pw@h.example:8443/p.json?x=1&token=abc&y=2")
        == "https://h.example:8443/p.json?x=1&token=***&y=2"
    )


def test_atomic_write_replaces_the_content(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(OLD, encoding="utf-8")

    atomic_write_text(path, '{"new": true}')

    assert path.read_text(encoding="utf-8") == '{"new": true}'
    assert _names(tmp_path) == ["profile.json"]  # no temporary file is left


@posix_only
def test_atomic_write_keeps_the_mode(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(OLD, encoding="utf-8")
    path.chmod(0o640)

    atomic_write_text(path, "{}")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
