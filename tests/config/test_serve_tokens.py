import hashlib
import importlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from rich.text import Text
from typer.testing import CliRunner

from sing_box_cli.config import serve_backend as backend, serve_tokens as tokens
from sing_box_cli.config.utils import request_get, subscription_credentials

SECRET = "test-subscription-secret"


@pytest.fixture
def protected_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, str, MagicMock]]:
    github = MagicMock(spec=backend.GitHub)
    github.config.return_value = b'{"outbounds":[{"type":"direct"}]}'
    monkeypatch.setattr(backend, "validate_config", lambda _: None)
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _: SECRET)
    server = backend.ConfigHTTPServer(("127.0.0.1", 0), tmp_path, github)
    domain = f"127.0.0.1:{server.server_port}"
    backend.save_source(
        tmp_path, backend.Source("https://github.com/owner/repo", domain)
    )
    tokens.create_token(tmp_path, "linux", domain, "client-linux.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tmp_path, f"http://{domain}", github
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/client-linux.json", {}),
        ("/client-linux.json", {"Authorization": "Bearer wrong"}),
        ("/client-linux.json", {"Authorization": "Bearer server-github-pat"}),
        ("/client-linux.json", {"Authorization": f"Basic {SECRET}"}),
        ("/client-linux.json", {"Authorization": "Bearer"}),
        ("/client-linux.json?token=", {}),
        ("/client-linux.json?token=wrong", {}),
        (f"/client-linux.json?token={SECRET}&token={SECRET}", {}),
        (f"/client-linux.json?token={SECRET}", {"Authorization": "Bearer wrong"}),
        (f"/client-linux.json?token={SECRET}", {"Authorization": "Basic wrong"}),
        ("/other.json", {"Authorization": f"Bearer {SECRET}"}),
        (
            "/client-linux.json",
            {"Authorization": f"Bearer {SECRET}", "Host": "other.example.com"},
        ),
    ],
)
def test_unauthorized_request_never_reads_github(
    protected_server: tuple[Path, str, MagicMock], path: str, headers: dict[str, str]
) -> None:
    _directory, url, github = protected_server
    response = httpx.get(url + path, headers=headers, trust_env=False)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.headers["Cache-Control"] == "no-store"
    assert SECRET not in response.text
    github.config.assert_not_called()


@pytest.mark.parametrize("mode", ["bearer", "query", "both"])
def test_subscription_credentials_allow_the_scoped_file(
    protected_server: tuple[Path, str, MagicMock], mode: str
) -> None:
    _directory, url, github = protected_server
    headers = {"Authorization": f"Bearer {SECRET}"} if mode != "query" else {}
    query = f"?token={SECRET}" if mode != "bearer" else ""
    response = httpx.get(
        url + "/client-linux.json" + query, headers=headers, trust_env=False
    )
    assert response.status_code == 200
    assert response.content == github.config.return_value
    assert response.headers["Referrer-Policy"] == "no-referrer"
    source, filename = github.config.call_args.args
    assert source.domain == httpx.URL(url).netloc.decode()
    assert filename == "client-linux.json"


def test_revocation_and_creation_take_effect_without_restart(
    protected_server: tuple[Path, str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, url, github = protected_server
    target = url + f"/client-linux.json?token={SECRET}"
    assert httpx.get(target, trust_env=False).status_code == 200
    tokens.revoke_token(directory, "linux")
    github.config.reset_mock()
    assert httpx.get(target, trust_env=False).status_code == 401
    github.config.assert_not_called()
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _: "new-secret")
    tokens.create_token(
        directory, "linux", httpx.URL(url).netloc.decode(), "client-linux.json"
    )
    assert (
        httpx.get(
            url + "/client-linux.json?token=new-secret", trust_env=False
        ).status_code
        == 200
    )
    assert httpx.get(target, trust_env=False).status_code == 401


@pytest.mark.parametrize("state", [None, "invalid json", '{"tokens":[{}]}'])
def test_missing_or_corrupt_token_settings_fail_closed(
    protected_server: tuple[Path, str, MagicMock], state: str | None
) -> None:
    directory, url, github = protected_server
    path = directory / "tokens.json"
    if state is None:
        path.unlink()
    else:
        path.write_text(state)
    response = httpx.get(url + f"/client-linux.json?token={SECRET}", trust_env=False)
    assert response.status_code == (401 if state is None else 503)
    github.config.assert_not_called()


def test_duplicate_authorization_headers_are_rejected(
    protected_server: tuple[Path, str, MagicMock],
) -> None:
    _directory, url, github = protected_server
    response = httpx.get(
        url + "/client-linux.json",
        headers=[
            ("Authorization", f"Bearer {SECRET}"),
            ("Authorization", f"Bearer {SECRET}"),
        ],
        trust_env=False,
    )
    assert response.status_code == 401
    github.config.assert_not_called()


def test_token_management_cli_stores_only_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "serve"
    monkeypatch.setenv("SBC_SERVE_DIR", str(directory))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    source = backend.Source("https://github.com/owner/repo", "sub.example.com")
    backend.save_source(directory, source)
    main = importlib.import_module("sing_box_cli.main")
    runner = CliRunner()
    args = [
        "config",
        "serve",
        "token",
        "create",
        "linux",
        "--domain",
        source.domain,
        "--file",
        "client-linux.json",
    ]
    result = runner.invoke(main.app, args)
    assert result.exit_code == 0, result.output
    secret = result.stdout.splitlines()[0].removeprefix("Token: ")
    assert len(secret) == 43
    assert (
        f"URL: https://sub.example.com/client-linux.json?token={secret}"
        in result.stdout
    )
    path = directory / "tokens.json"
    saved = path.read_text()
    assert secret not in saved
    assert hashlib.sha256(secret.encode()).hexdigest() in saved
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    duplicate = runner.invoke(main.app, args)
    assert duplicate.exit_code == 1
    assert path.read_text() == saved
    result = runner.invoke(main.app, ["config", "serve", "token", "list"])
    assert result.exit_code == 0
    assert "linux" in result.output and "client-linux.json" in result.output
    assert secret not in result.output
    assert hashlib.sha256(secret.encode()).hexdigest() not in result.output
    result = runner.invoke(main.app, ["config", "serve", "token", "revoke", "linux"])
    assert result.exit_code == 0
    assert tokens.load_tokens(directory) == []
    result = runner.invoke(main.app, ["config", "serve", "token", "revoke", "linux"])
    assert result.exit_code == 1
    result = runner.invoke(main.app, ["config", "serve", "token", "list"])
    assert "No subscription tokens" in result.output


@pytest.mark.parametrize(
    "name,domain,filename",
    [
        ("../bad", "sub.example.com", "client.json"),
        ("linux", "unregistered.example.com", "client.json"),
        ("linux", "sub.example.com", "../secret.json"),
    ],
)
def test_invalid_token_creation_does_not_save_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    domain: str,
    filename: str,
) -> None:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path))
    backend.save_source(
        tmp_path, backend.Source("https://github.com/owner/repo", "sub.example.com")
    )
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(
        main.app,
        [
            "config",
            "serve",
            "token",
            "create",
            name,
            "--domain",
            domain,
            "--file",
            filename,
        ],
    )
    assert result.exit_code == 1
    assert not (tmp_path / "tokens.json").exists()


def test_downloader_extracts_query_token_and_does_not_log_it(
    protected_server: tuple[Path, str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    directory, url, _github = protected_server
    target = url + f"/client-linux.json?token={SECRET}"
    assert request_get(target, "") is not None
    tokens.revoke_token(directory, "linux")
    assert request_get(target, "") is None
    output = capsys.readouterr().out
    assert SECRET not in output
    assert "401" in output


def test_query_token_preserves_other_parameters() -> None:
    clean_url, token = subscription_credentials(
        "https://sub.example.com/client.json?mode=test&token=my-secret&extra=1"
    )
    assert clean_url == "https://sub.example.com/client.json?mode=test&extra=1"
    assert token == "my-secret"


@pytest.mark.parametrize(
    "url,token",
    [
        ("https://sub.example.com/client.json?token=", None),
        ("https://sub.example.com/client.json?token=a&token=b", None),
        ("https://sub.example.com/client.json?token=a", "b"),
    ],
)
def test_invalid_or_conflicting_query_credentials_are_rejected(
    url: str, token: str | None
) -> None:
    with pytest.raises(ValueError):
        subscription_credentials(url, token)


def test_short_token_options_create_list_and_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path))
    backend.save_source(
        tmp_path, backend.Source("https://github.com/owner/repo", "sub.example.com")
    )
    main = importlib.import_module("sing_box_cli.main")
    forbidden = MagicMock(
        side_effect=AssertionError("Token management must not initialize client config")
    )
    monkeypatch.setattr(main, "get_config", forbidden)
    runner = CliRunner()
    result = runner.invoke(main.app, ["token", "-c", "client-linux.json"])
    assert result.exit_code == 0, result.output
    secret = result.stdout.splitlines()[0].removeprefix("Token: ")
    entry = tokens.load_tokens(tmp_path)[0]
    assert entry.filename == "client-linux.json" and entry.domain == "sub.example.com"
    assert secret not in (tmp_path / "tokens.json").read_text()
    result = runner.invoke(main.app, ["token", "-l"])
    assert result.exit_code == 0, result.output
    assert "client-linux.json" in result.output and secret not in result.output
    result = runner.invoke(main.app, ["token", "-r", secret])
    assert result.exit_code == 0, result.output
    assert "Revoked 1" in result.output
    assert secret not in result.output
    assert tokens.load_tokens(tmp_path) == []
    forbidden.assert_not_called()


@pytest.mark.parametrize("target", ["token", "file"])
def test_short_revoke_targets_token_or_file_without_restart(
    protected_server: tuple[Path, str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    directory, url, github = protected_server
    monkeypatch.setenv("SBC_SERVE_DIR", str(directory))
    domain = httpx.URL(url).netloc.decode()
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _: "second-secret")
    tokens.create_token(directory, "second", domain, "client-linux.json")
    monkeypatch.setattr(tokens.secrets, "token_urlsafe", lambda _: "other-secret")
    tokens.create_token(directory, "other", domain, "other.json")
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(
        main.app, ["token", "-r", SECRET if target == "token" else "client-linux.json"]
    )
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert (
        httpx.get(
            url + f"/client-linux.json?token={SECRET}", trust_env=False
        ).status_code
        == 401
    )
    second = httpx.get(url + "/client-linux.json?token=second-secret", trust_env=False)
    assert second.status_code == (200 if target == "token" else 401)
    assert (
        httpx.get(url + "/other.json?token=other-secret", trust_env=False).status_code
        == 200
    )
    assert len(tokens.load_tokens(directory)) == (2 if target == "token" else 1)
    assert github.config.called


def test_file_revoke_requires_domain_when_filename_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path))
    first = tokens.create_token(tmp_path, "first", "first.example.com", "client.json")
    tokens.create_token(tmp_path, "second", "second.example.com", "client.json")
    before = (tmp_path / "tokens.json").read_bytes()
    main = importlib.import_module("sing_box_cli.main")
    runner = CliRunner()
    result = runner.invoke(main.app, ["token", "-r", "client.json"])
    assert result.exit_code == 1
    assert "specify --domain" in result.output
    assert (tmp_path / "tokens.json").read_bytes() == before
    result = runner.invoke(
        main.app, ["token", "-r", "client.json", "-d", "first.example.com"]
    )
    assert result.exit_code == 0
    assert first not in result.output
    assert [entry.domain for entry in tokens.load_tokens(tmp_path)] == [
        "second.example.com"
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["-l", "-r", "client.json"],
        ["-c", "client.json", "-r", "client.json"],
        ["-c", "client.json"],
        ["-r", "missing-token"],
        ["-r", "missing.json"],
        ["-l", "-n", "invalid"],
        ["-l", "list"],
    ],
)
def test_invalid_short_token_actions_leave_credentials_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path))
    tokens.create_token(tmp_path, "existing", "sub.example.com", "client.json")
    before = (tmp_path / "tokens.json").read_bytes()
    main = importlib.import_module("sing_box_cli.main")
    result = CliRunner().invoke(main.app, ["token", *arguments])
    assert result.exit_code != 0
    assert (tmp_path / "tokens.json").read_bytes() == before


def test_nested_token_options_and_help_are_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBC_SERVE_DIR", str(tmp_path))
    backend.save_source(
        tmp_path, backend.Source("https://github.com/owner/repo", "sub.example.com")
    )
    main = importlib.import_module("sing_box_cli.main")
    runner = CliRunner()
    result = runner.invoke(
        main.app, ["config", "serve", "token", "-c", "client.json", "-n", "linux"]
    )
    assert result.exit_code == 0, result.output
    assert tokens.load_tokens(tmp_path)[0].name == "linux"
    result = runner.invoke(main.app, ["config", "serve", "token", "-l"])
    assert result.exit_code == 0 and "linux" in result.output
    monkeypatch.setenv("FORCE_COLOR", "1")
    for args in (["token"], ["token", "--help"]):
        result = runner.invoke(main.app, args)
        assert result.exit_code == 0, result.output
        help_text = Text.from_ansi(result.output).plain
        assert "--list" in help_text and "--revoke" in help_text
