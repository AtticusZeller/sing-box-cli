from typing import Annotated

import typer
from rich import print

from ..common import StrOrNone, ensure_root
from ..service import SharedContext, get_context_obj
from ..service.manager import create_service
from .config import get_config
from .serve import serve

__all__ = ["config"]

SubUrlArg = Annotated[str, typer.Argument(help="Subscription URL")]
TokenOption = Annotated[
    StrOrNone,
    typer.Option(
        "--token", "-t", help="Subscription access token (not the server's GitHub PAT)."
    ),
]
RestartServiceOption = Annotated[
    bool, typer.Option("--restart", "-r", help="Restart service after update.")
]
DryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run", help="Show configuration diff without saving or restarting."
    ),
]
config = typer.Typer(help="Configuration management commands")
config.add_typer(serve, name="serve")


@config.callback()
def config_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand not in ("serve", "update"):
        cfg = get_config(initialize=ctx.invoked_subcommand == "clear_cache")
        ctx.obj = SharedContext(config=cfg, service=create_service(cfg))


@config.command("update")
def config_update(
    url: SubUrlArg,
    token: TokenOption = None,
    restart: RestartServiceOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Download configuration or preview its diff with --dry-run."""
    cfg = get_config(initialize=not dry_run)
    if not cfg.update_config(url, token, dry_run=dry_run):
        print("❌ Failed to update configuration.")
        raise typer.Exit(1)
    if restart and not dry_run:
        ensure_root()
        service = create_service(cfg)
        # init service
        if not service.check_service():
            service.create_service()
            print("⌛ Service created successfully.")
        service.restart()


@config.command("get")
def config_get(
    ctx: typer.Context,
    subscription: Annotated[
        bool, typer.Option("--subscription", help="Print the subscription URL.")
    ] = False,
) -> None:
    """Print configuration, or select the subscription URL with --subscription."""
    cfg = get_context_obj(ctx).config
    if subscription:
        if cfg.sub_url:
            print(f"🔗 Current subscription URL: {cfg.sub_url}")
        else:
            print("❌ No subscription URL found.")
    else:
        print(cfg.config_file_content)


@config.command("show-sub", hidden=True)
def config_show_sub(ctx: typer.Context) -> None:
    """Compatibility alias for config get --subscription."""
    config_get(ctx, subscription=True)


@config.command("show", hidden=True)
def config_show(ctx: typer.Context) -> None:
    """Compatibility alias for config get."""
    config_get(ctx)


@config.command("clear_cache")
def config_clear_cache(ctx: typer.Context) -> None:
    """Clean cache database"""
    ctx_obj = get_context_obj(ctx)
    ctx_obj.config.clear_cache()
