"""Read-only GitHub configuration sources and HTTP delivery."""

import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
import typer
from sing_box_bin import get_bin_path

MAX_CONFIG_BYTES = 5 * 1024 * 1024


class ServeError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def state_directory() -> Path:
    override = os.environ.get("SBC_SERVE_DIR")
    return (
        Path(override).expanduser()
        if override
        else Path(typer.get_app_dir("sing-box-cli")) / "serve"
    ).resolve()


def normalize_repository(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ServeError("Use a repository URL: https://github.com/OWNER/REPO.", 400)
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 3
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", parts[1])
    ):
        raise ServeError("Use a repository URL: https://github.com/OWNER/REPO.", 400)
    repository = parts[2].removesuffix(".git")
    if repository in (".", "..") or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}", repository
    ):
        raise ServeError("Invalid GitHub repository name.", 400)
    return f"https://github.com/{parts[1]}/{repository}"


def normalize_domain(value: str) -> str:
    try:
        parsed = urlsplit(f"https://{value}")
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode().lower()
        port = parsed.port
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or value != value.strip()
            or any(char.isspace() for char in value)
            or len(host) > 253
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
        ):
            raise ValueError
        return host if port in (None, 443) else f"{host}:{port}"
    except (ValueError, UnicodeError):
        raise ServeError("Use a domain name without a scheme or path.", 400)


def valid_filename(name: str) -> bool:
    return (
        bool(name)
        and name.endswith(".json")
        and "/" not in name
        and "\\" not in name
        and all(ord(char) >= 32 for char in name)
    )


@dataclass(frozen=True)
class Source:
    repository: str
    domain: str

    @property
    def api_path(self) -> str:
        return (
            "/repos/"
            + self.repository.removeprefix("https://github.com/")
            + "/contents/"
        )

    def url(self, filename: str) -> str:
        return f"https://{self.domain}/{quote(filename, safe='')}"


def load_sources(directory: Path) -> list[Source]:
    path = directory / "sources.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sources = [
            Source(
                normalize_repository(item["repository"]),
                normalize_domain(item["domain"]),
            )
            for item in data["sources"]
        ]
        if len({source.domain for source in sources}) != len(sources):
            raise ValueError
        return sources
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ServeError):
        raise ServeError(f"Cannot read source settings: {path}.", 500)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_source(directory: Path, source: Source) -> None:
    sources = load_sources(directory)
    existing = next((item for item in sources if item.domain == source.domain), None)
    if existing and existing != source:
        raise ServeError(
            "This domain is already registered to another repository.", 400
        )
    if not existing:
        sources.append(source)
        write_json(
            directory / "sources.json", {"sources": [asdict(item) for item in sources]}
        )


def github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ServeError(
            "Set GH_TOKEN or GITHUB_TOKEN for private repository access.", 400
        )
    return token


class GitHub:
    def __init__(
        self, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=15,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, *, raw: bool = False) -> httpx.Response:
        try:
            response = self.client.get(
                path,
                headers={"Accept": "application/vnd.github.raw+json"} if raw else None,
            )
        except httpx.RequestError:
            raise ServeError("GitHub is unreachable or the request timed out.")
        if response.status_code == 404:
            raise ServeError(
                "GitHub file/repository not found or token has no access.", 404
            )
        if response.status_code in (403, 429):
            raise ServeError("GitHub access denied or API rate limit reached.", 503)
        if response.status_code == 401:
            raise ServeError("GitHub authentication failed.")
        if response.status_code != 200:
            raise ServeError(f"GitHub request failed (HTTP {response.status_code}).")
        return response

    def metadata(self, path: str) -> Any:
        try:
            return self.get(path).json()
        except ValueError:
            raise ServeError("GitHub returned an invalid response.")

    def filenames(self, source: Source) -> list[str]:
        entries = self.metadata(source.api_path)
        if not isinstance(entries, list):
            raise ServeError("GitHub did not return a repository directory.")
        return sorted(
            entry["name"]
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "file"
            and isinstance(entry.get("name"), str)
            and valid_filename(entry["name"])
            and entry.get("path") == entry["name"]
        )

    def config(self, source: Source, filename: str) -> bytes:
        if not valid_filename(filename):
            raise ServeError("Configuration not found.", 404)
        path = source.api_path + quote(filename, safe="")
        entry = self.metadata(path)
        if (
            not isinstance(entry, dict)
            or entry.get("type") != "file"
            or entry.get("path") != filename
            or entry.get("submodule_git_url")
        ):
            raise ServeError("Configuration not found.", 404)
        size = entry.get("size")
        if not isinstance(size, int) or size > MAX_CONFIG_BYTES:
            raise ServeError("Configuration exceeds the 5 MiB limit.", 413)
        try:
            if entry.get("encoding") == "base64" and isinstance(
                entry.get("content"), str
            ):
                content = base64.b64decode(
                    "".join(entry["content"].split()), validate=True
                )
            elif entry.get("encoding") == "none":
                content = self.get(path, raw=True).content
            else:
                raise ValueError
        except (ValueError, binascii.Error):
            raise ServeError("GitHub returned invalid file content.")
        if len(content) > MAX_CONFIG_BYTES:
            raise ServeError("Configuration exceeds the 5 MiB limit.", 413)
        return content


def validate_config(content: bytes) -> None:
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError):
        raise ServeError("Configuration is not valid JSON.", 422)
    if not isinstance(data, dict) or not data:
        raise ServeError("Configuration must be a non-empty JSON object.", 422)
    validation_content = content
    route = data.get("route")
    if isinstance(route, dict) and route.get("override_android_vpn") is True:
        # The server's core cannot initialize this Android-only option. Disable
        # it only in the check copy; HTTP responses retain the original bytes.
        route["override_android_vpn"] = False
        validation_content = json.dumps(data).encode()
    binary = str(get_bin_path())
    try:
        with tempfile.TemporaryDirectory(prefix="sbc-check-") as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(validation_content)
            result = subprocess.run(
                [binary, "check", "-c", str(path), "-D", directory],
                cwd=directory,
                capture_output=True,
                timeout=15,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise ServeError("sing-box configuration validation could not complete.", 503)
    if result.returncode:
        # Core errors may contain configuration secrets; never relay its output.
        raise ServeError("Configuration failed sing-box validation.", 422)


class ConfigHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], directory: Path, github: GitHub
    ) -> None:
        self.directory = directory
        self.github = github
        super().__init__(address, ConfigRequestHandler)


class ConfigRequestHandler(BaseHTTPRequestHandler):
    server: ConfigHTTPServer

    def do_GET(self) -> None:
        from .serve_tokens import authorize_subscription

        try:
            domain = normalize_domain(self.headers.get("Host", ""))
            parsed = urlsplit(self.path)
            filename = unquote(parsed.path.removeprefix("/"))
            authorize_subscription(
                self.server.directory,
                domain,
                filename,
                self.headers.get_all("Authorization", []),
                parsed.query,
            )
            source = next(
                (
                    item
                    for item in load_sources(self.server.directory)
                    if item.domain == domain
                ),
                None,
            )
            if source is None or not valid_filename(filename):
                raise ServeError("Configuration not found.", 404)
            content = self.server.github.config(source, filename)
            validate_config(content)
            self.reply(200, content)
        except ServeError as error:
            status = 404 if error.status == 400 else error.status
            self.reply(status, json.dumps({"error": str(error)}).encode())
        except (OSError, ValueError):
            self.reply(500, b'{"error":"Configuration service error."}')

    def reply(self, status: int, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="subscription"')
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log URLs, incoming Authorization headers or configuration bodies.
        pass
