import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer

from ..common import LogLevel, StrOrNone
from ..config.config import ConfigHandler
from ..service import get_context_obj
from .client import SingBoxAPIClient
from .connections import ConnectionsManager
from .logs import get_logs
from .monitor import ResourceMonitor, ResourceVisualizer
from .policy import PolicyGroupManager

ApiUrlOption = Annotated[
    StrOrNone,
    typer.Option(
        "--base-url",
        "-u",
        help="Base URL of the sing-box API, read from configuration file if not provided",
    ),
]
ApiTokenOption = Annotated[
    StrOrNone,
    typer.Option(
        "--token",
        "-t",
        help="Authentication token for the sing-box API, read from configuration file if not provided",
    ),
]

LogLevelOption = Annotated[
    LogLevel,
    typer.Option(
        "--log-level",
        "-l",
        help="Log level of trace, debug, info, warning, error, fatal, panic",
        case_sensitive=False,
    ),
]

api = typer.Typer(help="sing-box manager.")


def create_client(
    config: ConfigHandler, base_url: StrOrNone = None, token: StrOrNone = None
) -> SingBoxAPIClient:
    # read from config if not provided
    if base_url is None:
        base_url = config.api_base_url
    if token is None:
        token = config.api_secret
    return SingBoxAPIClient(base_url, token)


def _run_tui(
    ctx: typer.Context,
    command: Callable[[SingBoxAPIClient], Coroutine[Any, Any, None]],
    base_url: StrOrNone,
    token: StrOrNone,
) -> None:
    """Run an interactive command and handle user-requested exits consistently."""
    try:
        ctx_obj = get_context_obj(ctx)
        api_client = create_client(ctx_obj.config, base_url, token)
        asyncio.run(command(api_client))
    except (KeyboardInterrupt, EOFError):
        pass
    print("Exited.")


@api.command()
def stats(
    ctx: typer.Context, base_url: ApiUrlOption = None, token: ApiTokenOption = None
) -> None:
    """Show sing-box traffic, memory statistics and connections, requires API token(Optional)"""
    _run_tui(
        ctx,
        lambda client: ResourceMonitor(client, ResourceVisualizer()).start(),
        base_url,
        token,
    )


@api.command()
def conns(
    ctx: typer.Context, base_url: ApiUrlOption = None, token: ApiTokenOption = None
) -> None:
    """Manage sing-box connections, requires API token(Optional)"""
    _run_tui(ctx, lambda client: ConnectionsManager(client).run(), base_url, token)


@api.command()
def proxy(
    ctx: typer.Context, base_url: ApiUrlOption = None, token: ApiTokenOption = None
) -> None:
    """Manage sing-box policy groups, requires API token(Optional)"""
    _run_tui(ctx, lambda client: PolicyGroupManager(client).run(), base_url, token)


@api.command()
def logs(
    ctx: typer.Context,
    log_level: LogLevelOption = LogLevel.info,
    base_url: ApiUrlOption = None,
    token: ApiTokenOption = None,
) -> None:
    """Show sing-box logs, requires API token(Optional)"""
    print("⌛ Showing real-time logs (Press Ctrl+C to exit)")
    _run_tui(ctx, lambda client: get_logs(client, log_level), base_url, token)
