import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import with_token

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  remote_url TEXT,
  auto_update INTEGER NOT NULL,
  auto_update_interval_minutes INTEGER NOT NULL,
  last_updated INTEGER,
  item_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
  name TEXT PRIMARY KEY,
  data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  item_order INTEGER NOT NULL
);
"""


class SettingsError(Exception): ...


@dataclass(frozen=True)
class Profile:
    path: Path
    remote_url: str | None

    @property
    def id(self) -> str:
        return self.path.stem


@contextmanager
def invoking_user() -> Iterator[None]:
    if sys.platform == "win32":
        yield
        return
    uid, gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if os.geteuid() != 0 or not (uid.isdigit() and gid.isdigit()) or int(uid) == 0:
        yield
        return
    os.setegid(int(gid))
    os.seteuid(int(uid))
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)


def settings_path(config_dir: Path) -> Path:
    return config_dir / "settings.db"


def _settings_uri(path: Path, mode: str) -> str:
    """An SQLite URI that never creates the file, valid for Windows drive paths too."""
    return f"{path.absolute().as_uri()}?mode={mode}"


def settings_query(
    config_dir: Path, sql: str, params: tuple[str, ...] = ()
) -> list[tuple[Any, ...]]:
    path = settings_path(config_dir)
    if not path.is_file():
        raise SettingsError(
            f"The sing-box app's settings ({path}) do not exist, open the app once."
        )
    uri = _settings_uri(path, "ro")
    try:
        with invoking_user(), closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            rows: list[tuple[Any, ...]] = db.execute(sql, params).fetchall()
            return rows
    except sqlite3.Error as e:
        raise SettingsError(f"Cannot read the settings ({path}): {e}") from e


def _profile_of(config_dir: Path, profile_id: str) -> Profile:
    rows = settings_query(
        config_dir, "SELECT type, remote_url FROM profiles WHERE id = ?", (profile_id,)
    )
    if not rows:
        raise SettingsError(
            f"The selected profile {profile_id} is not in the settings."
        )
    path = config_dir / "profiles" / f"{profile_id}.json"
    if not path.is_file():
        raise SettingsError(f"Profile file {path} not found.")
    kind, url = rows[0]
    return Profile(path=path, remote_url=url if kind == "remote" and url else None)


def _decode_id(data: Any) -> str | None:
    try:
        value = json.loads(bytes(data))
    except (TypeError, ValueError):
        return None
    if isinstance(value, str) and value and Path(value).name == value:
        return value
    return None


def active_profile(config_dir: Path) -> Profile:
    rows = settings_query(
        config_dir, "SELECT data FROM preferences WHERE name = 'selected_profile_id'"
    )
    profile_id = _decode_id(rows[0][0]) if rows else None
    if profile_id is None:
        raise SettingsError("No profile is selected in the sing-box app.")
    return _profile_of(config_dir, profile_id)


def update_profile_record(
    config_dir: Path,
    profile_id: str,
    *,
    remote_url: str | None = None,
    make_remote: bool = False,
) -> None:
    """Record a refresh of a profile and, if given, its new subscription URL.

    ``last_updated`` is set as the app does after a refresh, ``remote_url`` when
    the subscription was changed from the command line, so everyone shares one
    subscription. ``make_remote`` turns a local profile into a remote one, which is
    only for a store sbc owns. A single UPDATE, SQLite serialises it with the app.
    """
    path = settings_path(config_dir)
    assignments = ["last_updated = ?"]
    params: list[int | str] = [int(time.time() * 1000)]
    if remote_url is not None:
        assignments.append("remote_url = ?")
        params.append(remote_url)
        if make_remote:
            assignments.append("type = 'remote'")
    where = "id = ?" if make_remote else "id = ? AND type = 'remote'"
    params.append(profile_id)
    uri = _settings_uri(path, "rw")
    try:
        with (
            invoking_user(),
            closing(sqlite3.connect(uri, uri=True, timeout=5)) as db,
            db,
        ):
            changed = db.execute(
                f"UPDATE profiles SET {', '.join(assignments)} WHERE {where}", params
            ).rowcount
    except sqlite3.Error as e:
        raise SettingsError(f"Cannot update the settings ({path}): {e}") from e
    if not changed:
        raise SettingsError(f"Profile {profile_id} is not a remote profile.")


def _legacy_subscription(config_dir: Path) -> str:
    legacy_app = config_dir / "app_config.json"
    if not legacy_app.is_file():
        return ""
    try:
        saved = json.loads(legacy_app.read_text(encoding="utf-8"))
        url = str(saved.get("subscription_url", "")).strip()
        token = str(saved.get("token", "")).strip()
    except (OSError, ValueError, AttributeError):
        return ""
    return with_token(url, token) if url and token else url


def legacy_profile(config_dir: Path) -> Profile:
    return Profile(
        path=config_dir / "config.json",
        remote_url=_legacy_subscription(config_dir) or None,
    )


def init_store(config_dir: Path) -> Profile:
    path = settings_path(config_dir)
    imported = False
    try:
        with invoking_user():
            (config_dir / "profiles").mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch(mode=0o600)
            with closing(sqlite3.connect(path, timeout=5)) as db, db:
                db.executescript(_SCHEMA)
                profile_id = _usable_selection(db, config_dir)
                if profile_id is None:
                    profile_id = _import(db, config_dir)
                    imported = True
            profile = _profile_of(config_dir, profile_id)
            if imported:
                # only once the store is committed: nothing is lost if this fails
                _retire_legacy_files(config_dir, profile.path)
    except (sqlite3.Error, OSError) as e:
        raise SettingsError(f"Cannot use the settings ({path}): {e}") from e
    return profile


def _usable_selection(db: sqlite3.Connection, config_dir: Path) -> str | None:

    def whole(profile_id: str) -> bool:
        known = db.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,))
        return (
            known.fetchone() is not None
            and (config_dir / "profiles" / f"{profile_id}.json").is_file()
        )

    row = db.execute(
        "SELECT data FROM preferences WHERE name = 'selected_profile_id'"
    ).fetchone()
    selected = _decode_id(row[0]) if row else None
    if selected and whole(selected):
        return selected
    for (profile_id,) in db.execute("SELECT id FROM profiles ORDER BY item_order"):
        if whole(profile_id):
            _select(db, profile_id)
            return str(profile_id)
    return None


def _select(db: sqlite3.Connection, profile_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO preferences VALUES ('selected_profile_id', ?)",
        (json.dumps(profile_id).encode(),),
    )


def _import(db: sqlite3.Connection, config_dir: Path) -> str:
    legacy_config = config_dir / "config.json"
    content = "{}"
    if legacy_config.is_file():
        content = legacy_config.read_text(encoding="utf-8")
    url = _legacy_subscription(config_dir)

    profile_id = str(uuid.uuid4())
    path = config_dir / "profiles" / f"{profile_id}.json"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    order = db.execute(
        "SELECT COALESCE(MAX(item_order) + 1, 0) FROM profiles"
    ).fetchone()
    db.execute(
        "INSERT INTO profiles (id, name, type, remote_url, auto_update, "
        "auto_update_interval_minutes, last_updated, item_order) "
        "VALUES (?, 'main', ?, ?, 0, 60, NULL, ?)",
        (profile_id, "remote" if url else "local", url or None, order[0]),
    )
    _select(db, profile_id)
    return profile_id


def _retire_legacy_files(config_dir: Path, profile: Path) -> None:
    legacy_app = config_dir / "app_config.json"
    if legacy_app.is_file():
        os.replace(legacy_app, legacy_app.with_name("app_config.json.migrated"))

    legacy_config = config_dir / "config.json"
    if not (legacy_config.is_file() or legacy_config.is_symlink()):
        return
    backup = legacy_config.with_name("config.json.migrated")
    os.replace(legacy_config, backup)
    try:
        legacy_config.symlink_to(profile)
    except OSError:
        os.replace(backup, legacy_config)


__all__ = [
    "Profile",
    "SettingsError",
    "active_profile",
    "init_store",
    "invoking_user",
    "legacy_profile",
    "settings_path",
    "settings_query",
    "update_profile_record",
]
