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
from .utils import request_get, show_diff_config, subscription_credentials


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
    """Load and save configuration files."""

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

    def save(self, config_path: Path) -> None:
        """Save configuration to file."""
        config_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


class SingBoxConfig(ConfigModel):
    """Model representing the sing-box configuration file structure."""

    # We only model the parts we interact with directly
    experimental: ExperimentalConfig | None = Field(default=None)

    # Other fields will be preserved but not explicitly modeled
    model_config = ConfigDict(extra="allow")


class SingBoxAppConfig(ConfigModel):
    """Application-specific configuration for sing-box-cli."""

    subscription_url: str = Field(default="", description="Subscription URL")
    token: str = Field(default="", description="Authentication token")


class ConfigHandler:
    def __init__(self) -> None:
        self._config_dir = (
            Path(typer.get_app_dir("sing-box", roaming=True))
            if self.is_windows
            else Path(f"~{self.user}/.config/sing-box").expanduser()
        )
        # Configs file path
        self._config_file = self._config_dir / "config.json"
        self._app_config_file = self._config_dir / "app_config.json"

        # Load both configurations
        self._sing_box_config = SingBoxConfig.load(self._config_file)
        self._app_config = SingBoxAppConfig.load(self._app_config_file)
        print(self)

    def init_directories(self) -> bool:
        """Initialize necessary directories and files for sing-box."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)

            # Create/save default configs if needed
            if not self._config_file.exists():
                self._sing_box_config.save(self._config_file)
                print(f"📁 Created config file: {self._config_file}")

            if not self._app_config_file.exists():
                self._app_config.save(self._app_config_file)
                print(f"📁 Created application config file: {self._app_config_file}")

            # For Linux/Unix systems only - set proper ownership
            if not self.is_windows:
                for file in [self._config_file, self._app_config_file]:
                    if file.exists():
                        shutil.chown(file, user=self.user, group=self.user)
                shutil.chown(self.config_dir, user=self.user, group=self.user)
            return True
        except Exception as e:
            print(f"❌ Failed to initialize directories: {e}")
            return False

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
        return (
            self._config_file.read_text(encoding="utf-8")
            if self._config_file.exists()
            else "{}"
        )

    @config_file_content.setter
    def config_file_content(self, value: str) -> None:
        """Set the content of the configuration file and reload the model."""
        try:
            self._sing_box_config = SingBoxConfig.model_validate_json(value)
            self._sing_box_config.save(self._config_file)
        except Exception as e:
            print(f"⚠️ Error parsing new configuration: {e}")
        print("📁 Configuration updated successfully.")

    @property
    def sub_url(self) -> str:
        """Get the subscription URL from app config."""
        return self._app_config.subscription_url

    @sub_url.setter
    def sub_url(self, value: str) -> None:
        """Set the subscription URL in app config."""
        self._app_config.subscription_url = value.strip()
        self._app_config.save(self._app_config_file)
        print("📁 Subscription updated successfully.")

    @property
    def token_content(self) -> str:
        """Get the token from app config."""
        return self._app_config.token

    @token_content.setter
    def token_content(self, value: str) -> None:
        """Set the token in app config."""
        self._app_config.token = value.strip()
        self._app_config.save(self._app_config_file)
        print("🔑 Token added successfully.")

    def update_config(
        self,
        sub_url: StrOrNone = None,
        token: StrOrNone = None,
        *,
        dry_run: bool = False,
    ) -> bool:
        """Download and diff configuration, optionally without saving changes."""
        try:
            if sub_url is None:
                # load from file
                if not self.sub_url:
                    print("❌ No subscription URL found.")
                    return False
                sub_url = self.sub_url
            sub_url, token = subscription_credentials(sub_url, token)
            if token is None:
                # load from file
                token = self.token_content
            action = "Previewing" if dry_run else "Updating"
            print(f"⌛ {action} configuration from {sub_url}")
            response = request_get(sub_url, token)
            if response is None:
                print("❌ Failed to get configuration.")
                return False

            new_config = response.text

            # make sure same order of keys to avoid showing wrong diff
            new_config = self._sing_box_config.model_validate_json(
                new_config
            ).model_dump_json(indent=2)
            if self.config_file_content == new_config:
                print("📄 Configuration is up to date.")
            else:
                # show differences before saving
                show_diff_config(self.config_file_content, new_config)

            if dry_run:
                print("📄 Dry run: no configuration or subscription changes saved.")
                return True

            if not self.is_windows:
                shutil.chown(self._config_file, user=self.user, group=self.user)

            if self.config_file_content != new_config:
                self.config_file_content = new_config

            # Update subscription url file
            if sub_url != self.sub_url:
                self.sub_url = sub_url
            if token != self.token_content:
                self.token_content = token
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
