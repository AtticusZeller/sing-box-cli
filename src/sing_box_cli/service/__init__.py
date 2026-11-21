from dataclasses import dataclass
from typing import Annotated, cast

import typer

from ..common import ClearCacheOption, UpdateConfigOption, ensure_root
from ..config.config import ConfigHandler
from .manager import ServiceManager

__all__ = ["service", "SharedContext", "get_context_obj", "ensure_privileges"]


service = typer.Typer(help="Service management commands")

_PURGE_DATA_LOSS = (
    "Tailscale node state (log in again), Taildrop files, dashboard data "
    "and the daemon's saved configuration"
)
PurgeDataOption = Annotated[
    bool,
    typer.Option(
        "--purge-data",
        help=f'Desktop app: delete the daemon\'s working directory, like "clear data" '
        f"in the app. Loses {_PURGE_DATA_LOSS}. Asks first.",
    ),
]
YesOption = Annotated[
    bool, typer.Option("--yes", "-y", help="Do not ask before --purge-data.")
]


@dataclass
class SharedContext:
    config: ConfigHandler
    service: ServiceManager


def get_context_obj(ctx: typer.Context) -> SharedContext:
    return cast(SharedContext, ctx.obj)


def ensure_privileges(service: ServiceManager) -> None:
    if service.requires_root:
        ensure_root()


@service.command(
    "enable", help="Start the service; enable autostart for standalone installations."
)
def service_enable(ctx: typer.Context) -> None:
    ctx_obj = get_context_obj(ctx)
    config = ctx_obj.config
    service = ctx_obj.service
    ensure_privileges(service)
    service.enable_service()
    print("🔥 Service started.")
    if config.api_base_url:
        print(f"🔌 Default API: {config.api_base_url}")


@service.command(
    "disable", help="Stop the service; disable autostart for standalone installations."
)
def service_disable(ctx: typer.Context) -> None:
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    result = service.disable_service()
    print(f"✋ {result}")


@service.command(
    "restart",
    help="Restart sing-box service, update configuration if needed, create service if not exists",
)
def service_restart(
    ctx: typer.Context,
    update: UpdateConfigOption = False,
    clear_cache: ClearCacheOption = False,
    purge_data: PurgeDataOption = False,
    yes: YesOption = False,
) -> None:
    ctx_obj = get_context_obj(ctx)
    config = ctx_obj.config
    service = ctx_obj.service
    ensure_privileges(service)
    if purge_data:
        if not service.supports_purge_data:
            print("❌ --purge-data is only for the sing-box desktop app.")
            raise typer.Exit(1)
        if not yes:
            typer.confirm(
                f"Delete the desktop daemon's working directory? "
                f"This loses {_PURGE_DATA_LOSS}",
                abort=True,
            )
    elif clear_cache:
        ensure_root()
    if service.check_service():
        service.sync_service()
    else:
        service.create_service()
    if update:
        if config.update_config():
            pass
        else:
            print("❌ Failed to update configuration.")
            raise typer.Exit(1)
    if service.restarts_in_place and not (clear_cache or purge_data):
        service.restart()
    else:
        service.stop()
        if purge_data:
            service.purge_data()
        elif clear_cache:
            service.clear_cache()
        service.start()
    print("🔥 Service restarted.")
    if config.api_base_url:
        print(f"🔌 Default API: {config.api_base_url}")


@service.command("stop", help="Stop sing-box service")
def service_stop(ctx: typer.Context) -> None:
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    service.stop()
    print("✋ Service stopped.")


@service.command("status", help="Check service status")
def service_status(ctx: typer.Context) -> None:
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    status = service.status()
    print(f"🏃 Service status: {status}")
