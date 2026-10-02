import json
from difflib import unified_diff
from pathlib import Path
from typing import Any

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


def request_get(url: str, token: str) -> httpx.Response | None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers)
        response.raise_for_status()
        return response
    except Exception as e:
        print(f"❌ Failed to get from {url}: {e}")
        return None
