import importlib
import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from sing_box_cli import installation


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installation.sys, "platform", "linux")
    monkeypatch.setattr(installation.os, "geteuid", lambda: 1000, raising=False)


@pytest.mark.usefixtures("linux")
def test_install_elevates_only_file_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    config = MagicMock(side_effect=AssertionError("installation must not load config"))
    monkeypatch.setattr(main, "get_config", config)
    calls = []

    def capture(args: list[str], *, check: bool) -> None:
        assert check
        assert args[:6] == ["sudo", "install", "-m", "755", "-T", "--"]
        calls.append((Path(args[-1]).name, Path(args[-2]).read_text()))

    monkeypatch.setattr(installation.subprocess, "run", capture)
    result = CliRunner().invoke(main.app, ["install", "--bin-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls] == list(installation.ENTRY_POINTS)
    assert all(" -B -c " in content for _, content in calls)
    config.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Uses Linux install and shell")
def test_install_migrates_symlinks_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installation.os, "geteuid", lambda: 0)
    target = tmp_path / "old environment" / "sbc"
    target.parent.mkdir()
    target.write_text("original entry point")
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in installation.ENTRY_POINTS:
        (directory / name).symlink_to(target)
    installation.install_launchers(directory)
    installation.install_launchers(directory)
    assert target.read_text() == "original entry point"
    for name in installation.ENTRY_POINTS:
        destination = directory / name
        assert not destination.is_symlink()
        assert destination.stat().st_mode & 0o777 == 0o755
        assert destination.read_text() == installation.launcher_content()


@pytest.mark.usefixtures("linux")
@pytest.mark.parametrize("kind", ["file", "directory", "link"])
def test_install_checks_both_destinations_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    destination = tmp_path / "sbc"
    if kind == "file":
        destination.write_bytes(b"unrelated\xff")
    elif kind == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(tmp_path / "another-tool")
    run = MagicMock()
    monkeypatch.setattr(installation.subprocess, "run", run)
    with pytest.raises(typer.BadParameter, match="unrelated"):
        installation.install_launchers(tmp_path)
    run.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Uses a POSIX launcher")
def test_launcher_disables_bytecode_before_imports_and_forwards_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A symlinked venv interpreter must retain its path, including shell quotes.
    environment = tmp_path / "tool env's directory"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / "bin" / "python"
    monkeypatch.setattr(installation.sys, "executable", str(python))
    site_packages = next((environment / "lib").glob("python*/site-packages"))
    module = site_packages / "sing_box_cli.py"
    module.write_text(
        "import sys\n"
        "def main():\n"
        "    assert sys.dont_write_bytecode\n"
        "    print(repr(sys.argv[1:]))\n"
    )
    launcher = tmp_path / "sbc"
    launcher.write_text(installation.launcher_content())
    launcher.chmod(0o755)
    # A same-named module in the working directory must not shadow the tool.
    (tmp_path / "sing_box_cli.py").write_text("raise RuntimeError('shadowed CLI')\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    arguments = ["path with spaces", "quote's", "$(echo no)", "--option"]
    result = subprocess.run(
        [str(launcher), *arguments],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == repr(arguments)
    assert not (site_packages / "__pycache__").exists()
    assert f"exec {shlex.quote(str(python))} -I -B" in launcher.read_text()


@pytest.mark.usefixtures("linux")
def test_failed_sudo_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    run = MagicMock(side_effect=subprocess.CalledProcessError(1, "sudo"))
    monkeypatch.setattr(installation.subprocess, "run", run)
    result = CliRunner().invoke(main.app, ["install", "--bin-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Failed to run sudo" in result.output
    assert "Installed" not in result.output


def test_unsupported_platform_does_not_load_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    monkeypatch.setattr(installation.sys, "platform", "win32")
    config = MagicMock()
    monkeypatch.setattr(main, "get_config", config)
    result = CliRunner().invoke(main.app, ["install"])
    assert result.exit_code == 2
    assert "Linux only" in result.output
    config.assert_not_called()
