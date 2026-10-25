from dataclasses import dataclass
from typing import cast

import typer

from ..common import ClearCacheOption, UpdateConfigOption, ensure_root
from ..config.config import ConfigHandler
from .manager import ServiceManager

__all__ = ["service", "SharedContext", "get_context_obj", "ensure_privileges"]


service = typer.Typer(help="Service management commands")


@dataclass
class SharedContext:
    config: ConfigHandler
    service: ServiceManager


def get_context_obj(ctx: typer.Context) -> SharedContext:
    return cast(SharedContext, ctx.obj)


def ensure_privileges(service: ServiceManager) -> None:
    """Require root only for backends that manage a system service themselves."""
    if service.requires_root:
        ensure_root()


@service.command("enable")
def service_enable(ctx: typer.Context) -> None:
    """Start the service; enable autostart for standalone installations."""
    ctx_obj = get_context_obj(ctx)
    config = ctx_obj.config
    service = ctx_obj.service
    ensure_privileges(service)
    service.enable_service()
    print("🔥 Service started.")
    if config.api_base_url:
        print(f"🔌 Default API: {config.api_base_url}")


@service.command("disable")
def service_disable(ctx: typer.Context) -> None:
    """Stop the service; disable autostart for standalone installations."""
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    result = service.disable_service()
    print(f"✋ {result}")


@service.command("restart")
def service_restart(
    ctx: typer.Context,
    update: UpdateConfigOption = False,
    purge: ClearCacheOption = False,
) -> None:
    """Restart sing-box service, update configuration if needed, create service if not exists"""
    ctx_obj = get_context_obj(ctx)
    config = ctx_obj.config
    service = ctx_obj.service
    ensure_privileges(service)
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
    if service.hot_reload and not purge:
        service.restart()
    else:
        service.stop()
        if purge:
            service.clear_cache()
        service.start()
    print("🔥 Service restarted.")
    if config.api_base_url:
        print(f"🔌 Default API: {config.api_base_url}")


@service.command("stop")
def service_stop(ctx: typer.Context) -> None:
    """Stop sing-box service"""
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    service.stop()
    print("✋ Service stopped.")


@service.command("status")
def service_status(ctx: typer.Context) -> None:
    """Check service status"""
    ctx_obj = get_context_obj(ctx)
    service = ctx_obj.service
    ensure_privileges(service)
    status = service.status()
    print(f"🏃 Service status: {status}")
