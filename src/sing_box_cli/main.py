import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich import print

from sing_box_cli.api import api as api_app
from sing_box_cli.common import ClearCacheOption, UpdateConfigOption, ensure_root
from sing_box_cli.config import config as config_app
from sing_box_cli.config.config import get_config, run_args
from sing_box_cli.config.serve import token_options
from sing_box_cli.installation import install_launchers
from sing_box_cli.service import SharedContext, get_context_obj, service as service_app
from sing_box_cli.service.manager import create_service

app = typer.Typer(help="sing-box manager.")
app.add_typer(api_app)
app.add_typer(service_app, name="service")
app.add_typer(config_app, name="config")
app.command("token")(token_options)


@app.callback(invoke_without_command=False)
def callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand in ("install", "config", "token"):
        return
    cfg = get_config()
    service = create_service(cfg)
    ctx.obj = SharedContext(config=cfg, service=service)


@app.command("install")
def install(
    directory: Annotated[
        Path, typer.Option("--bin-dir", help="Existing system executable directory.")
    ] = Path("/usr/local/bin"),
) -> None:
    """Install both system CLI entry points, requesting sudo when needed."""
    install_launchers(directory)


@app.command()
def run(
    ctx: typer.Context,
    update: UpdateConfigOption = False,
    purge: ClearCacheOption = False,
    force_cli: Annotated[
        bool,
        typer.Option(
            "--force-cli",
            help="Linux: stop the desktop app and services, then debug with the standalone core.",
        ),
    ] = False,
) -> None:
    """Run sing-box if host's service unavailable or debug mode."""
    cfg = get_context_obj(ctx).config
    if force_cli:
        import sys

        if sys.platform != "linux":
            print("❌ --force-cli is supported on Linux only.")
            raise typer.Exit(1)
    if cfg.external_config is True:
        if force_cli:
            from sing_box_cli.service.debug import run_desktop_debug

            ensure_root()
            run_desktop_debug(cfg, update=update, purge=purge)
            return
        action = "the desktop app" if cfg.is_windows else "`sbc service restart`"
        print(f"❌ The sing-box desktop app runs the core, use {action}.")
        if not cfg.is_windows:
            print("ℹ️ For foreground debugging, use: sudo sbc run --force-cli")
        raise typer.Exit(1)
    ensure_root()

    if update:
        if cfg.update_config():
            pass
        else:
            print("❌ Failed to update configuration.")
            raise typer.Exit(1)
    if purge:
        cfg.clear_cache()

    # stop if empty
    if cfg.config_file_content == "{}":
        print("❌ Configuration file is empty.")
        raise typer.Exit(1)

    # run
    cmd = run_args(cfg)
    subprocess.run(cmd)


@app.command()
def version(ctx: typer.Context) -> None:
    """Show version"""
    from sing_box_cli import __version__

    print(f"🔖 sing-box-cli {__version__}")
    print(f"📦 {get_context_obj(ctx).service.version()}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
