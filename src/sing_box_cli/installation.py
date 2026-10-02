"""System launchers that avoid privileged writes to Python environments."""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

ENTRY_POINTS = ("sing-box-cli", "sbc")
LAUNCHER_MARKER = "# Managed by sing-box-cli; prevents privileged bytecode writes."


def _require_linux() -> None:
    if sys.platform != "linux":
        raise typer.BadParameter("This command is supported on Linux only.")


def _privileged_run(args: list[str]) -> None:
    if os.geteuid() != 0:
        args = ["sudo", *args]
    try:
        subprocess.run(args, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to run {args[0]}: {exc}", err=True)
        raise typer.Exit(1) from exc


def launcher_content() -> str:
    # Do not resolve the interpreter symlink: its venv path selects dependencies.
    python = shlex.quote(os.path.abspath(sys.executable))
    code = shlex.quote("from sing_box_cli import main; main()")
    # Isolated mode keeps the working directory and PYTHONPATH out of imports.
    return f'#!/bin/sh\n{LAUNCHER_MARKER}\nexec {python} -I -B -c {code} "$@"\n'


def install_launchers(directory: Path) -> None:
    _require_linux()
    directory = directory.absolute()
    if not directory.is_dir():
        raise typer.BadParameter(f"Installation directory does not exist: {directory}")
    destinations = [directory / name for name in ENTRY_POINTS]
    # Validate both destinations before replacing either one.
    for destination in destinations:
        if destination.is_symlink():
            if destination.resolve().name not in ENTRY_POINTS:
                raise typer.BadParameter(
                    f"Refusing to replace unrelated link: {destination}"
                )
        elif destination.exists():
            header = f"#!/bin/sh\n{LAUNCHER_MARKER}\n".encode()
            if not destination.is_file() or not destination.read_bytes().startswith(
                header
            ):
                raise typer.BadParameter(
                    f"Refusing to replace unrelated file: {destination}"
                )
    with tempfile.TemporaryDirectory(prefix="sbc-install-") as temporary:
        source = Path(temporary) / "launcher"
        source.write_text(launcher_content())
        for destination in destinations:
            _privileged_run(
                ["install", "-m", "755", "-T", "--", str(source), str(destination)]
            )
            typer.echo(f"Installed {destination}")
