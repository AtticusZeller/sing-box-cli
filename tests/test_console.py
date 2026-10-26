"""Exercise real encoded streams: CliRunner normally hides encoding failures."""

import importlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sing_box_cli import __version__
from sing_box_cli.config.config import ConfigHandler

main = importlib.import_module("sing_box_cli.main")


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
@pytest.mark.parametrize("desktop", [False, True])
@pytest.mark.parametrize("command", [["version"], ["service", "status"]])
def test_command_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    encoding: str,
    desktop: bool,
    command: list[str],
) -> None:
    # Keep real ConfigHandler construction, but isolate all filesystem/service state.
    monkeypatch.setattr(ConfigHandler, "is_windows", property(lambda self: True))
    monkeypatch.setattr("typer.get_app_dir", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(ConfigHandler, "_detect_desktop_app", lambda self: desktop)
    service = Mock()
    service.requires_root = False
    service.version.return_value = "sing-box test-core"
    service.status.return_value = "running"
    monkeypatch.setattr(main, "create_service", lambda cfg: service)
    monkeypatch.setattr(sys, "argv", ["sbc", *command])
    stdout = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stdout)
        patch.setattr(sys, "stderr", stderr)
        # Simulate the entry point's OS decision without changing pathlib/Rich's OS.
        patch.setattr(
            main,
            "sys",
            SimpleNamespace(platform="win32", stdout=stdout, stderr=stderr),
            raising=False,
        )
        with pytest.raises(SystemExit) as exited:
            main.main()
        assert exited.value.code == 0
        stdout.flush()
        stderr.flush()
        output = stdout.buffer.getvalue().decode(encoding)
        errors = stderr.buffer.getvalue().decode(encoding)
    assert errors == ""
    assert "Using " not in output
    lines = output.splitlines()
    if command == ["version"]:
        assert not (tmp_path / "settings.db").exists()
        assert len(lines) == 2
        assert lines[0].endswith(f"sing-box-cli {__version__}")
        assert lines[1].endswith("sing-box test-core")
        service.version.assert_called_once_with()
    else:
        assert "Service status:" in output
        service.status.assert_called_once_with()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_output_encoding_policy(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    streams = [
        io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
        for encoding in ("cp1252", "utf-8")
    ]
    monkeypatch.setattr(
        main,
        "sys",
        SimpleNamespace(platform=platform, stdout=streams[0], stderr=streams[1]),
    )
    main._configure_console_output()
    assert [stream.encoding for stream in streams] == ["cp1252", "utf-8"]
    for stream in streams:
        assert stream.errors == ("replace" if platform == "win32" else "strict")
    if platform == "win32":
        for stream in streams:
            stream.write("🔧 中文 café")
            stream.flush()
        assert streams[0].buffer.getvalue() == "? ?? café".encode("cp1252")
        assert streams[1].buffer.getvalue().decode("utf-8") == "🔧 中文 café"


@pytest.mark.parametrize("stream", [None, io.StringIO(), Mock(reconfigure=None)])
def test_stream_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch, stream: object
) -> None:
    stderr = Mock()
    monkeypatch.setattr(
        main, "sys", SimpleNamespace(platform="win32", stdout=stream, stderr=stderr)
    )
    main._configure_console_output()
    stderr.reconfigure.assert_called_once_with(errors="replace")


@pytest.mark.parametrize("error", [OSError, ValueError, io.UnsupportedOperation])
def test_unavailable_stream_does_not_block_stderr(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    stdout, stderr = Mock(), Mock()
    stdout.reconfigure.side_effect = error
    monkeypatch.setattr(
        main, "sys", SimpleNamespace(platform="win32", stdout=stdout, stderr=stderr)
    )
    main._configure_console_output()
    stderr.reconfigure.assert_called_once_with(errors="replace")
