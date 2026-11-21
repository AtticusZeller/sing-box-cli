import os
from enum import Enum
from typing import Annotated

import typer

StrOrNone = str | None
UpdateConfigOption = Annotated[
    bool, typer.Option("--update", "-u", help="Update configuration before running.")
]
ClearCacheOption = Annotated[
    bool, typer.Option("--clear-cache", "-cc", help="Clear cache.db before running.")
]


class LogLevel(str, Enum):
    trace = "trace"
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"
    fatal = "fatal"
    panic = "panic"


def ensure_root() -> None:
    try:
        if os.geteuid() != 0:  # type: ignore
            print("⚠️ This script must be run as root.")
            raise typer.Exit(1)
    except AttributeError:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore
            print("⚠️ This script must be run as Administrator.")
            raise typer.Exit(1)
