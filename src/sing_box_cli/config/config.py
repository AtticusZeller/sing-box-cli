import dataclasses
import json
import os
import platform
import shutil
from pathlib import Path
from typing import Self

import typer
from pydantic import BaseModel, ConfigDict, Field
from rich import print
from sing_box_bin import get_bin_path

from ..common import StrOrNone
from .store import (
    Profile,
    SettingsError,
    active_profile,
    init_store,
    legacy_profile,
    settings_path,
    update_profile_record,
)
from .utils import (
    atomic_write_text,
    redact_url,
    request_get,
    show_diff_config,
    subscription_credentials,
    with_token,
)


class ClashApiConfig(BaseModel):
    """Clash API configuration settings."""

    external_controller: str = Field(default="127.0.0.1:9090")
    default_mode: str = Field(default="Rule")
    secret: str = Field(default="")


class CacheFileConfig(BaseModel):
    """Cache file configuration settings."""

    enabled: bool = Field(default=True)
    path: str = Field(default="cache.db")


class ExperimentalConfig(BaseModel):
    """Experimental features configuration."""

    cache_file: CacheFileConfig | None = Field(default=None)
    clash_api: ClashApiConfig | None = Field(default=None)


class ConfigModel(BaseModel):
    """Load configuration files."""

    @classmethod
    def load(cls, config_path: Path) -> Self:
        """Load configuration from file or create with defaults if not exists."""
        if config_path.exists():
            try:
                return cls.model_validate(
                    json.loads(config_path.read_text(encoding="utf-8"))
                )
            except Exception as e:
                print(f"⚠️ Error loading app config, using defaults: {e}")
                return cls()
        return cls()


class SingBoxConfig(ConfigModel):
    """Model representing the sing-box configuration file structure."""

    # We only model the parts we interact with directly
    experimental: ExperimentalConfig | None = Field(default=None)

    # Other fields will be preserved but not explicitly modeled
    model_config = ConfigDict(extra="allow")


class ConfigHandler:
    def __init__(self) -> None:
        self._config_dir = (
            Path(typer.get_app_dir("sing-box", roaming=True))
            if self.is_windows
            else Path(f"~{self.user}/.config/sing-box").expanduser()
        )
        if self.is_windows:
            from ..service.desktop_windows import application_data_directory

            try:
                self._config_dir = application_data_directory(self._config_dir)
            except SettingsError as e:
                print(f"❌ {e}")
                raise typer.Exit(1) from e
        # Everything lives in the profile store (see config.store): the selected profile
        # is the configuration and its subscription URL is the subscription. With the
        # sing-box desktop app installed the store is the app's: it is only read, and an
        # unusable one is reported, never worked around. Without the app the store is
        # ours and init_directories() creates it.
        self._desktop_app = self._detect_desktop_app()
        self._profile: Profile | None = None
        self._profile_error: str | None = None
        # False while showing the files of an older version, before our store exists
        self._store_ready = False
        try:
            self._profile = active_profile(self._config_dir)
            self._store_ready = True
        except SettingsError as e:
            if self._desktop_app:
                self._profile_error = str(e)
            else:
                self._profile = legacy_profile(self._config_dir)
        self._load_config_file()
        print(self)

    def _detect_desktop_app(self) -> bool:
        # Lazy import: the service package imports this module.
        from ..service.desktop import desktop_app_installed

        try:
            return desktop_app_installed()
        except SettingsError as e:
            print(f"❌ {e}")
            raise typer.Exit(1) from e

    def _load_config_file(self) -> None:
        # never read or written without a profile, see _require_profile()
        self._config_file = (
            self._profile.path
            if self._profile
            else self._config_dir / "profiles" / "unavailable.json"
        )
        self._sing_box_config = SingBoxConfig.load(self._config_file)

    def _require_profile(self) -> Profile:
        """The selected profile, or report why there is none and exit."""
        if self._profile is None:
            print(f"❌ {self._profile_error}")
            raise typer.Exit(1)
        return self._profile

    def _require_store(self) -> Profile:
        """The selected profile of a store that may be written."""
        profile = self._require_profile()
        if not self._store_ready:
            raise RuntimeError("The profile store is not initialized.")
        return profile

    @property
    def external_config(self) -> bool:
        """Whether the sing-box desktop app owns the configuration."""
        return self._desktop_app

    @property
    def legacy_config(self) -> Path | None:
        """config.json of an older version that is still a file of its own.

        It normally becomes a link to the profile. Where linking is not allowed a
        service created before still runs it, until it is pointed at the profile.
        """
        path = self._config_dir / "config.json"
        if self._desktop_app or not self._store_ready:
            return None
        return path if path.is_file() and not path.is_symlink() else None

    def init_directories(self) -> bool:
        """Create the profile store if it is ours to create (no desktop app)."""
        if self._desktop_app or self._store_ready:
            return True
        created = not settings_path(self._config_dir).exists()
        try:
            self._profile = init_store(self._config_dir)
        except SettingsError as e:
            print(f"❌ Failed to initialize directories: {e}")
            return False
        self._store_ready = True
        self._load_config_file()
        if created:
            print(f"📁 Created profile store: {settings_path(self._config_dir)}")
        return True

    @property
    def user(self) -> str:
        """Get the current username from environment variables."""
        user = (
            os.environ.get("SUDO_USER")
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
        )
        if not user:
            raise ValueError("❌ Unable to detect user name")
        return user

    @property
    def bin_path(self) -> Path:
        """Get the path of the sing-box binary."""
        # sing-box-beta for linux beta version
        bin_path: Path | str | None = shutil.which("sing-box") or shutil.which(
            "sing-box-beta"
        )

        if not bin_path:
            bin_path = get_bin_path()
        return Path(bin_path).absolute()

    @property
    def is_windows(self) -> bool:
        """Check if the current platform is Windows."""
        return platform.system() == "Windows"

    @property
    def config_dir(self) -> Path:
        """Get the configuration directory path."""
        return self._config_dir.absolute()

    @property
    def cache_db(self) -> Path:
        """Get the cache database path, parsed from config if available."""
        if (
            self._sing_box_config.experimental
            and self._sing_box_config.experimental.cache_file
            and self._sing_box_config.experimental.cache_file.path
        ):
            cache_path = self._sing_box_config.experimental.cache_file.path
            # If it's a relative path, make it relative to config dir
            if not Path(cache_path).is_absolute():
                return self._config_dir / cache_path
            return Path(cache_path)

        # Default fallback
        return self._config_dir / "cache.db"

    @property
    def api_base_url(self) -> str:
        """Get the API base URL from the configuration file."""
        self._require_profile()
        if (
            self._sing_box_config.experimental
            and self._sing_box_config.experimental.clash_api
            and self._sing_box_config.experimental.clash_api.external_controller
        ):
            url = self._sing_box_config.experimental.clash_api.external_controller
            if url and not url.startswith("http"):
                return f"http://{url}"
            return url
        return ""

    @property
    def api_secret(self) -> str:
        """Get the API secret from the configuration file."""
        if (
            self._sing_box_config.experimental
            and self._sing_box_config.experimental.clash_api
            and self._sing_box_config.experimental.clash_api.secret
        ):
            return self._sing_box_config.experimental.clash_api.secret
        return ""

    @property
    def config_file(self) -> Path:
        """Get the path of the configuration file."""
        return self._config_file

    @property
    def config_file_content(self) -> str:
        """Get the content of the configuration file."""
        self._require_profile()
        return (
            self._config_file.read_text(encoding="utf-8")
            if self._config_file.exists()
            else "{}"
        )

    @config_file_content.setter
    def config_file_content(self, value: str) -> None:
        """Write the configuration as given and reload the model."""
        self._require_store()
        self._sing_box_config = SingBoxConfig.model_validate_json(value)
        # the bytes as downloaded: the desktop app compares them on its own refresh
        atomic_write_text(self._config_file, value)
        print("📁 Configuration updated successfully.")

    @staticmethod
    def _normalized(text: str, *, strict: bool = False) -> str:
        """One layout to compare configurations in, so formatting is no difference."""
        try:
            return SingBoxConfig.model_validate_json(text).model_dump_json(indent=2)
        except ValueError:
            if strict:
                raise
            return text

    @property
    def _stored_sub_url(self) -> str:
        """The subscription as stored: with its token, like the desktop app keeps it."""
        return self._require_profile().remote_url or ""

    def _subscription(self) -> tuple[str, str]:
        stored = self._stored_sub_url
        try:
            url, token = subscription_credentials(stored)
        except ValueError:
            return redact_url(stored), ""
        return url, token or ""

    @property
    def sub_url(self) -> str:
        """Get the subscription URL, without its token."""
        return self._subscription()[0]

    @property
    def token_content(self) -> str:
        """Get the subscription token, which is kept inside the stored URL."""
        return self._subscription()[1]

    def _save_subscription(self, stored: str) -> None:
        profile = self._require_store()
        update_profile_record(
            self._config_dir,
            profile.id,
            remote_url=stored,
            # a local profile of the app is not ours to turn into a remote one
            make_remote=not self._desktop_app,
        )
        self._profile = dataclasses.replace(profile, remote_url=stored)
        print("📁 Subscription updated successfully.")

    def _record_refresh(self) -> None:
        """Show the refresh in the store, the app lists when a profile was updated."""
        if not (self._profile and self._profile.remote_url):
            return
        try:
            update_profile_record(self._config_dir, self._profile.id)
        except SettingsError as e:
            print(f"⚠️ Could not record the refresh: {e}")

    def update_config(
        self,
        sub_url: StrOrNone = None,
        token: StrOrNone = None,
        *,
        dry_run: bool = False,
    ) -> bool:
        """Download and diff configuration, optionally without saving changes."""
        profile = self._require_profile()  # exits when the desktop app has none
        if self._desktop_app and profile.remote_url is None and sub_url is not None:
            print(
                "❌ The selected profile in the sing-box app is a local profile, "
                "it has no subscription to set."
            )
            return False
        try:
            # the saved subscription unless another one is given
            given = (self._stored_sub_url if sub_url is None else sub_url).strip()
            if not given:
                print("❌ No subscription URL found.")
                return False
            sub_url, token = subscription_credentials(given, token)
            if token is None:
                # the token of the saved subscription
                token = self.token_content
            action = "Previewing" if dry_run else "Updating"
            print(f"⌛ {action} configuration from {redact_url(sub_url)}")
            response = request_get(sub_url, token)
            if response is None:
                print("❌ Failed to get configuration.")
                return False

            new_config = response.text

            # make sure same order of keys to avoid showing wrong diff
            current = self._normalized(self.config_file_content)
            proposed = self._normalized(new_config, strict=True)
            if current == proposed:
                print("📄 Configuration is up to date.")
            else:
                # show differences before saving
                show_diff_config(current, proposed)

            if dry_run:
                print("📄 Dry run: no configuration or subscription changes saved.")
                return True

            if current != proposed:
                self.config_file_content = new_config

            # Save the subscription once it is known to work
            if (sub_url, token) != (self.sub_url, self.token_content):
                if given != sub_url:
                    stored = given  # the token came inside the URL: keep it as given
                else:
                    stored = with_token(sub_url, token) if token else sub_url
                self._save_subscription(stored)
            self._record_refresh()
            return True
        except Exception as e:
            print(f"❌ Failed to update configuration: {e}")
            return False

    def clear_cache(self) -> None:
        """Remove the cache database file."""
        try:
            self.cache_db.unlink(missing_ok=False)
            print("🗑️ Cache database removed.")
        except FileNotFoundError:
            print("❌ Cache database not found.")
        except PermissionError:
            print(
                "❌ Permission denied to remove cache database. Stop the service first."
            )
        except Exception as e:
            print(f"❌ Failed to remove cache database: {e}")

    def __str__(self) -> str:
        """Return a string representation of the configuration."""
        if self._desktop_app:
            return "🔧 Using sing-box desktop app\n" + (
                f"📄 Using configuration: {self._profile.path}"
                if self._profile
                else f"⚠️ {self._profile_error}"
            )
        info = (
            f"🔧 Using binary: {self.bin_path}\n"
            f"📄 Using configuration: {self._config_file}\n"
            f"💾 Using cache: {self.cache_db}"
        )
        return info


def get_config(*, initialize: bool = True) -> ConfigHandler:
    """Load configuration, optionally initializing local directories and files."""
    config = ConfigHandler()
    if initialize and not config.init_directories():
        raise FileNotFoundError("❌ Failed to initialize directories")
    return config


def run_args(config: ConfigHandler) -> list[str]:
    """Build subprocess arguments without splitting paths containing whitespace."""
    return [
        str(config.bin_path),
        "run",
        "-c",
        str(config.config_file),
        "-D",
        str(config.config_dir),
    ]


def run_cmd(config: ConfigHandler) -> str:
    return f"{config.bin_path} run -c {config.config_file} -D {config.config_dir}"
