import json
from difflib import unified_diff
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import getproxies

import httpx
from rich import print


def show_diff_config(current_config: str, new_config: str) -> None:
    print("📄 Configuration differences:")

    diff = list(
        unified_diff(
            current_config.splitlines(),
            new_config.splitlines(),
            fromfile="old",
            tofile="new",
            lineterm="",
        )
    )

    for line in diff:
        if line.startswith("+"):
            print(f"[green]{line}[/green]")
        elif line.startswith("-"):
            print(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            print(f"[blue]{line}[/blue]")
        else:
            print(f"[white]{line}[/white]")


def load_json_asdict(config_file: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(config_file.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"❌ Failed to load configuration: {e}")
        return {}


def subscription_credentials(
    url: str, token: str | None = None
) -> tuple[str, str | None]:
    """Move a URL's subscription token out of the URL before logging or saving it."""
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=32)
    query_tokens = [value for key, value in pairs if key == "token"]
    if not query_tokens:
        return url, token
    if (
        len(query_tokens) != 1
        or not query_tokens[0]
        or len(query_tokens[0]) > 512
        or any(char.isspace() for char in query_tokens[0])
    ):
        raise ValueError("Use exactly one non-empty subscription token in the URL.")
    url_token = query_tokens[0]
    if token is not None and token != url_token:
        raise ValueError("The URL token and --token differ; supply only one token.")
    query = urlencode([(key, value) for key, value in pairs if key != "token"])
    return urlunsplit(parsed._replace(query=query)), url_token


def request_get(url: str, token: str) -> httpx.Response | None:
    # Strip credentials before any error can include the request URL.
    safe_url = "subscription URL"
    try:
        url, resolved_token = subscription_credentials(url, token or None)
        safe_url = url
        headers = {"Content-Type": "application/json"}
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        try:
            response = httpx.get(url, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # A custom transport disables HTTPX's environment proxy detection.
            # Never bypass a configured proxy when retrying a failed connection.
            proxies = getproxies()
            if any(proxies.get(scheme) for scheme in ("http", "https", "all")):
                raise
            print("⚠️ Connection failed; retrying over IPv4.")
            transport = httpx.HTTPTransport(local_address="0.0.0.0")
            with httpx.Client(transport=transport) as client:
                response = client.get(url, headers=headers)
        response.raise_for_status()
        return response
    except Exception as e:
        print(f"❌ Failed to get from {safe_url}: {e}")
        return None
