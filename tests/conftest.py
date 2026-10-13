import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def config_path() -> None:
    cfg_path = Path(__file__).parent / "data/config.json"
    if not cfg_path.exists() and not cfg_path.is_file():
        raise FileNotFoundError(f"File not found: {cfg_path}")
    return cfg_path


@pytest.fixture
def make_settings_db() -> Callable[..., Path]:
    """Build (or extend) a copy of the sing-box desktop app's settings database."""

    def make(
        config_dir: Path,
        *,
        selected: str | None = None,
        profiles: dict[str, str | None] | None = None,
        preferences: dict[str, object] | None = None,
    ) -> Path:
        """``profiles`` maps id to subscription URL, None for a local profile."""
        path = config_dir / "settings.db"
        db = sqlite3.connect(path)
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS preferences "
                "(name TEXT PRIMARY KEY, data BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, type TEXT NOT NULL, remote_url TEXT, "
                "auto_update INTEGER NOT NULL, "
                "auto_update_interval_minutes INTEGER NOT NULL, "
                "last_updated INTEGER, item_order INTEGER NOT NULL)"
            )
            prefs = dict(preferences or {})
            if selected is not None:
                prefs["selected_profile_id"] = selected
            for name, value in prefs.items():
                db.execute(
                    "INSERT OR REPLACE INTO preferences VALUES (?, ?)",
                    (name, json.dumps(value).encode()),
                )
            for order, (profile_id, url) in enumerate((profiles or {}).items()):
                db.execute(
                    "INSERT OR REPLACE INTO profiles VALUES (?, 'main', ?, ?, ?, 60, NULL, ?)",
                    (
                        profile_id,
                        "remote" if url else "local",
                        url,
                        int(bool(url)),
                        order,
                    ),
                )
        db.close()
        return path

    return make
