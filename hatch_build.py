"""Bundle the small Windows SCM helper; the CLI remains Python."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        go = shutil.which("go")
        if go is None:
            raise RuntimeError(
                "Building a wheel requires Go (see windows-service/go.mod). Installing a published wheel does not require Go."
            )
        root = Path(self.root)
        output = root / "build" / "windows-service"
        output.mkdir(parents=True, exist_ok=True)
        # Both native resources travel in the same cross-platform Python wheel.
        # They are executables, not Python extension modules or CLI replacements.
        for architecture in ("amd64", "arm64"):
            filename = f"sbc-service-windows-{architecture}.exe"
            destination = output / filename
            subprocess.run(
                [
                    go,
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-ldflags=-s -w",
                    "-o",
                    str(destination),
                    ".",
                ],
                cwd=root / "windows-service",
                env={
                    **os.environ,
                    "GOOS": "windows",
                    "GOARCH": architecture,
                    "CGO_ENABLED": "0",
                },
                check=True,
            )
            build_data["force_include"][str(destination)] = (
                f"sing_box_cli/bin/{filename}"
            )
