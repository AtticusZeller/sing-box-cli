import base64
import importlib
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import psutil
import pytest
from typer.testing import CliRunner

from sing_box_cli.config import serve_backend as backend, serve_tokens
from sing_box_cli.config.serve import (
    ProcessState,
    control_lock,
    matching_process,
    process_state,
    start_server,
    stop_server,
)

SOURCE = backend.Source("https://github.com/owner/private-configs", "sub.example.com")
VALID_CONFIG = b'{"outbounds":[{"type":"direct","tag":"direct"}]}'
ANDROID_CONFIG = (
    b'{\n  "route": {"override_android_vpn": true},\n'
    b'  "outbounds": [{"type": "direct", "tag": "direct"}]\n}\n'
)


def file_entry(filename: str, content: bytes) -> dict[str, object]:
    return {
        "type": "file",
        "name": filename,
        "path": filename,
        "size": len(content),
        "encoding": "base64",
        "content": base64.encodebytes(content).decode(),
    }


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path / "serve"))
    monkeypatch.setenv("GH_TOKEN", "server-only-token")
    main = importlib.import_module("sing_box_cli.main")
    config = importlib.import_module("sing_box_cli.config")
    forbidden = MagicMock(
        side_effect=AssertionError("Must not initialize client config")
    )
    monkeypatch.setattr(main, "get_config", forbidden)
    monkeypatch.setattr(config, "get_config", forbidden)
    return tmp_path / "serve"


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/owner/repo/tree/main",
        "https://token@github.com/owner/repo",
        "https://other.example.com/owner/repo",
        "http://github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "https://[invalid/owner/repo",
    ],
)
def test_repository_url_rejects_ambiguous_or_credential_urls(value: str) -> None:
    with pytest.raises(backend.ServeError):
        backend.normalize_repository(value)


def test_domain_and_repository_normalization() -> None:
    assert (
        backend.normalize_repository("https://github.com/owner/repo.git/")
        == "https://github.com/owner/repo"
    )
    assert backend.normalize_domain("SUB.example.com.:443") == "sub.example.com"
    for value in (
        "https://sub.example.com",
        "sub.example.com/path",
        "a@sub.example.com",
        "sub.exa\nmple.com",
        "sub.example.com:bad",
    ):
        with pytest.raises(backend.ServeError):
            backend.normalize_domain(value)


def test_github_auth_and_root_file_filtering() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer private-token"
        if request.url.path.endswith("/contents/"):
            return httpx.Response(
                200,
                json=[
                    file_entry("alice.json", VALID_CONFIG),
                    {"type": "dir", "name": "nested.json", "path": "nested.json"},
                    {"type": "symlink", "name": "link.json", "path": "link.json"},
                    {
                        "type": "file",
                        "name": "nested.json",
                        "path": "directory/nested.json",
                    },
                    file_entry("README.md", b"readme"),
                ],
            )
        assert request.url.path.endswith("/contents/alice.json")
        return httpx.Response(200, json=file_entry("alice.json", VALID_CONFIG))

    github = backend.GitHub("private-token", httpx.MockTransport(respond))
    try:
        assert github.filenames(SOURCE) == ["alice.json"]
        assert github.config(SOURCE, "alice.json") == VALID_CONFIG
    finally:
        github.close()


@pytest.mark.parametrize(
    "status,expected", [(401, 502), (403, 503), (404, 404), (429, 503), (500, 502)]
)
def test_github_errors_never_relay_upstream_details(status: int, expected: int) -> None:
    github = backend.GitHub(
        "secret",
        httpx.MockTransport(
            lambda _: httpx.Response(status, text="secret token upstream details")
        ),
    )
    try:
        with pytest.raises(backend.ServeError) as error:
            github.filenames(SOURCE)
        assert error.value.status == expected
        assert "secret" not in str(error.value)
    finally:
        github.close()


def test_large_file_uses_raw_api_without_download_url() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        if request.headers["Accept"] == "application/vnd.github.raw+json":
            return httpx.Response(200, content=VALID_CONFIG)
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "large.json",
                "size": 1048577,
                "encoding": "none",
                "download_url": "https://untrusted.example/",
            },
        )

    github = backend.GitHub("secret", httpx.MockTransport(respond))
    try:
        assert github.config(SOURCE, "large.json") == VALID_CONFIG
    finally:
        github.close()


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.json",
        "nested/file.json",
        "file.json\x00",
        "config.txt",
        "nested\\file.json",
    ],
)
def test_unsafe_file_requests_do_not_reach_github(filename: str) -> None:
    transport = MagicMock(spec=httpx.BaseTransport)
    github = backend.GitHub("secret", transport)
    try:
        with pytest.raises(backend.ServeError) as error:
            github.config(SOURCE, filename)
        assert error.value.status == 404
        transport.handle_request.assert_not_called()
    finally:
        github.close()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"token":"secret"}',
        b'{"outbounds":[{"type":"not-real"}]}',
        b'{"route":{"override_android_vpn":true},"token":"secret"}',
        b'{"route":{"override_android_vpn":true},"outbounds":[{"type":"not-real"}]}',
        b'{"route":{"override_android_vpn":"true"}}',
        b'{"route":{"override_android_vpn":1}}',
    ],
)
def test_real_core_rejects_non_configurations(content: bytes) -> None:
    with pytest.raises(backend.ServeError) as error:
        backend.validate_config(content)
    assert error.value.status == 422
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("content", [VALID_CONFIG, ANDROID_CONFIG])
def test_real_core_accepts_configuration(content: bytes) -> None:
    backend.validate_config(content)


def test_registration_and_list_cli(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    cli = importlib.import_module("sing_box_cli.config.serve")
    files = {
        "alice.json": VALID_CONFIG,
        "android.json": ANDROID_CONFIG,
        "app.json": b'{"token":"secret"}',
    }
    github = backend.GitHub(
        "server-only-token",
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[file_entry(name, content) for name, content in files.items()]
                if request.url.path.endswith("/contents/")
                else file_entry(
                    request.url.path.rsplit("/", 1)[-1],
                    files[request.url.path.rsplit("/", 1)[-1]],
                ),
            )
        ),
    )
    monkeypatch.setattr(cli, "GitHub", lambda _token: github)
    monkeypatch.setattr(github, "close", lambda: None)
    runner = CliRunner()
    try:
        arguments = [
            "config",
            "serve",
            "add",
            SOURCE.repository,
            "--domain",
            SOURCE.domain,
        ]
        for _ in range(2):
            result = runner.invoke(main.app, arguments)
            assert result.exit_code == 0, result.output
        assert backend.load_sources(cli_environment) == [SOURCE]
        assert "server-only-token" not in (cli_environment / "sources.json").read_text()
        result = runner.invoke(main.app, ["config", "serve", "list"])
        assert result.exit_code == 0, result.output
        assert "https://sub.example.com/alice.json" in result.stdout
        assert "https://sub.example.com/android.json" in result.stdout
        assert "https://sub.example.com/app.json" not in result.stdout
        assert "Skipped app.json" in result.stderr
        assert "secret" not in result.output
        conflict = runner.invoke(
            main.app,
            [
                "config",
                "serve",
                "add",
                "https://github.com/owner/other",
                "--domain",
                SOURCE.domain,
            ],
        )
        assert conflict.exit_code == 1
        assert backend.load_sources(cli_environment) == [SOURCE]
    finally:
        github.client.close()


def test_no_sources_and_missing_token(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    runner = CliRunner()
    result = runner.invoke(main.app, ["config", "serve", "list"])
    assert result.exit_code == 0 and "No repositories" in result.output
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = runner.invoke(
        main.app,
        ["config", "serve", "add", SOURCE.repository, "--domain", SOURCE.domain],
    )
    assert result.exit_code == 1 and "Set GH_TOKEN" in result.output
    assert not (cli_environment / "sources.json").exists()


def test_registration_failure_does_not_save_source(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    cli = importlib.import_module("sing_box_cli.config.serve")
    github = backend.GitHub(
        "secret", httpx.MockTransport(lambda _: httpx.Response(404))
    )
    monkeypatch.setattr(cli, "GitHub", lambda _: github)
    result = CliRunner().invoke(
        main.app,
        ["config", "serve", "add", SOURCE.repository, "--domain", SOURCE.domain],
    )
    assert result.exit_code == 1
    assert not (cli_environment / "sources.json").exists()


def test_existing_update_arguments_and_restart_are_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = importlib.import_module("sing_box_cli.main")
    config = importlib.import_module("sing_box_cli.config")
    cfg = MagicMock()
    service = MagicMock()
    monkeypatch.setattr(config, "get_config", lambda **_: cfg)
    monkeypatch.setattr(config, "create_service", lambda _: service)
    monkeypatch.setattr(config, "ensure_root", lambda: None)
    result = CliRunner().invoke(
        main.app,
        [
            "config",
            "update",
            "https://sub.example.com/alice.json",
            "--token",
            "existing-token",
            "--restart",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg.update_config.assert_called_once_with(
        "https://sub.example.com/alice.json", "existing-token", dry_run=False
    )
    service.restart.assert_called_once()


@pytest.fixture
def http_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, dict[str, bytes]]]:
    backend.save_source(tmp_path, SOURCE)
    files = {"alice.json": VALID_CONFIG}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer server-only-token"
        filename = request.url.path.rsplit("/", 1)[-1]
        if filename not in files:
            return httpx.Response(404)
        return httpx.Response(200, json=file_entry(filename, files[filename]))

    github = backend.GitHub("server-only-token", httpx.MockTransport(respond))
    server = backend.ConfigHTTPServer(("127.0.0.1", 0), tmp_path, github)
    backend.save_source(
        tmp_path, backend.Source(SOURCE.repository, f"127.0.0.1:{server.server_port}")
    )
    monkeypatch.setattr(
        serve_tokens.secrets, "token_urlsafe", lambda _: "old-client-token"
    )
    serve_tokens.create_token(
        tmp_path, "alice", f"127.0.0.1:{server.server_port}", "alice.json"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", files
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        github.close()


def test_http_passthrough_updates_deletions_and_validation(
    http_service: tuple[str, dict[str, bytes]],
) -> None:
    url, files = http_service
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer old-client-token"},
        trust_env=False,
    ) as client:
        response = client.get("/alice.json")
        assert response.status_code == 200 and response.content == VALID_CONFIG
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        files["alice.json"] = b'{"outbounds":[{"type":"direct","tag":"changed"}]}'
        assert client.get("/alice.json").content == files["alice.json"]
        files["alice.json"] = b'{"token":"secret"}'
        response = client.get("/alice.json")
        assert response.status_code == 422 and "secret" not in response.text
        del files["alice.json"]
        assert client.get("/alice.json").status_code == 404
        assert client.get("/%2E%2E%2Fsecret.json").status_code == 401
        assert (
            client.get("/alice.json", headers={"Host": "other.example.com"}).status_code
            == 401
        )


def test_existing_downloader_requires_subscription_token(
    http_service: tuple[str, dict[str, bytes]],
) -> None:
    from sing_box_cli.config.utils import request_get

    url, _files = http_service
    assert request_get(f"{url}/alice.json", "") is None
    response = request_get(f"{url}/alice.json", "old-client-token")
    assert response is not None and response.content == VALID_CONFIG


def test_http_returns_android_configuration_unchanged(
    http_service: tuple[str, dict[str, bytes]], tmp_path: Path
) -> None:
    url, files = http_service
    files["android.json"] = ANDROID_CONFIG
    serve_tokens.create_token(
        tmp_path, "android", httpx.URL(url).netloc.decode(), "android.json"
    )
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer old-client-token"},
        trust_env=False,
    ) as client:
        response = client.get("/android.json")
        assert response.status_code == 200
        assert response.content == ANDROID_CONFIG


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_detached_start_stop_and_stale_state(cli_environment: Path) -> None:
    backend.save_source(cli_environment, SOURCE)
    port = free_port()
    main = importlib.import_module("sing_box_cli.main")
    runner = CliRunner()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from sing_box_cli import main; main()",
                "config",
                "serve",
                "start",
                "--port",
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        state = process_state(cli_environment)
        assert state is not None and state.port == port
        assert matching_process(cli_environment, state) is not None
        result = runner.invoke(
            main.app, ["config", "serve", "start", "--port", str(port)]
        )
        assert result.exit_code == 0 and "Already running" in result.output
        assert process_state(cli_environment) == state
        # Host is not registered: a live response without making any GitHub request.
        response = httpx.get(f"http://127.0.0.1:{port}/missing.json", trust_env=False)
        assert response.status_code == 401
        for _ in range(2):
            result = runner.invoke(main.app, ["config", "serve", "stop"])
            assert result.exit_code == 0, result.output
        assert process_state(cli_environment) is None
        assert matching_process(cli_environment, state) is None
        unrelated = psutil.Process()
        stale = ProcessState(
            pid=os.getpid(),
            created_at=unrelated.create_time(),
            instance="stale",
            host="127.0.0.1",
            port=port,
        )
        backend.write_json(cli_environment / "process.json", stale.model_dump())
        assert stop_server(cli_environment) is False
        assert unrelated.is_running()
        # A stale PID does not prevent a new instance from starting.
        backend.write_json(cli_environment / "process.json", stale.model_dump())
        new_state, started = start_server(cli_environment, "127.0.0.1", port)
        assert started and new_state.instance != stale.instance
    finally:
        stop_server(cli_environment)


def test_busy_port_reports_start_failure_without_pid(cli_environment: Path) -> None:
    backend.save_source(cli_environment, SOURCE)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(backend.ServeError, match="failed to start"):
            start_server(cli_environment, "127.0.0.1", listener.getsockname()[1])
    assert process_state(cli_environment) is None
    assert (
        "Address already in use" in (cli_environment / "serve.log").read_text()
        or os.name == "nt"
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Same-process lock semantics differ on Windows"
)
def test_concurrent_lifecycle_command_is_rejected(cli_environment: Path) -> None:
    with control_lock(cli_environment):
        with pytest.raises(backend.ServeError, match="Another serve command"):
            with control_lock(cli_environment):
                pytest.fail("Must not acquire the same lock twice")
