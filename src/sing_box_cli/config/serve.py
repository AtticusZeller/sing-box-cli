"""CLI registration and detached process lifecycle for config delivery."""

import contextlib
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import psutil
import typer
from pydantic import BaseModel, Field, ValidationError

from .serve_backend import (
    GitHub,
    ServeError,
    Source,
    github_token,
    load_sources,
    normalize_domain,
    normalize_repository,
    save_source,
    state_directory,
    validate_config,
)
from .serve_tokens import create_token, load_tokens, revoke_subscription_tokens

WORKER_MODULE = "sing_box_cli.config._serve_worker"
serve = typer.Typer(help="Serve private GitHub configurations through your own domain.")


class ProcessState(BaseModel):
    pid: int = Field(gt=0)
    created_at: float
    instance: str
    host: str
    port: int = Field(ge=1, le=65535)


@contextlib.contextmanager
def control_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "control.lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ServeError(
                    "Another serve command is running; try again shortly.", 409
                )
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ServeError(
                    "Another serve command is running; try again shortly.", 409
                )
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def process_state(directory: Path) -> ProcessState | None:
    try:
        return ProcessState.model_validate_json(
            (directory / "process.json").read_text()
        )
    except (FileNotFoundError, ValidationError):
        return None


def matching_process(
    directory: Path, state: ProcessState | None
) -> psutil.Process | None:
    if state is None:
        return None
    try:
        process = psutil.Process(state.pid)
        if (
            process.create_time() != state.created_at
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return None
        args = process.cmdline()
        if (
            WORKER_MODULE not in args
            or str(directory) not in args
            or state.instance not in args
        ):
            return None
        return process if process.is_running() else None
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        raise ServeError(
            "Cannot verify the service process; run as its owning user.", 403
        )


def start_server(directory: Path, host: str, port: int) -> tuple[ProcessState, bool]:
    with control_lock(directory):
        previous = process_state(directory)
        if matching_process(directory, previous) is not None:
            assert previous is not None
            return previous, False
        (directory / "process.json").unlink(missing_ok=True)
        if not load_sources(directory):
            raise ServeError("Register a repository with config serve add first.", 400)
        github_token()
        instance = uuid.uuid4().hex
        command = [
            sys.executable,
            "-B",
            "-m",
            WORKER_MODULE,
            "--state-dir",
            str(directory),
            "--host",
            host,
            "--port",
            str(port),
            "--instance",
            instance,
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        with (directory / "serve.log").open("ab") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=directory,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = process_state(directory)
            if state is not None and state.instance == instance:
                if matching_process(directory, state) is not None:
                    return state, True
            if child.poll() is not None:
                break
            time.sleep(0.05)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        (directory / "process.json").unlink(missing_ok=True)
        raise ServeError(
            f"Service failed to start. See {directory / 'serve.log'}.", 500
        )


def stop_server(directory: Path) -> bool:
    with control_lock(directory):
        process = matching_process(directory, process_state(directory))
        if process is None:
            (directory / "process.json").unlink(missing_ok=True)
            return False
        try:
            process.terminate()
            process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                raise ServeError("Service has not stopped yet; try stop again.", 500)
        (directory / "process.json").unlink(missing_ok=True)
        return True


@contextlib.contextmanager
def command_errors() -> Iterator[None]:
    try:
        yield
    except (ServeError, OSError, psutil.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise typer.Exit(1)


@serve.command("add")
def add(
    github_url: Annotated[str, typer.Argument(help="https://github.com/OWNER/REPO")],
    domain: Annotated[str, typer.Option("--domain", help="Public Traefik domain.")],
) -> None:
    """Register a repository after verifying server-side GitHub access."""
    with command_errors():
        source = Source(normalize_repository(github_url), normalize_domain(domain))
        github = GitHub(github_token())
        try:
            github.filenames(source)
        finally:
            github.close()
        directory = state_directory()
        with control_lock(directory):
            save_source(directory, source)
        print(f"Registered {source.repository} → https://{source.domain}")


@serve.command("list")
def list_configs() -> None:
    """Print valid configuration URLs from registered repositories."""
    with command_errors():
        sources = load_sources(state_directory())
        if not sources:
            print("No repositories registered.")
            return
        github = GitHub(github_token())
        urls: list[str] = []
        try:
            for source in sources:
                for filename in github.filenames(source):
                    try:
                        validate_config(github.config(source, filename))
                    except ServeError as error:
                        if error.status not in (413, 422):
                            raise
                        print(f"Skipped {filename}: {error}", file=sys.stderr)
                        continue
                    urls.append(source.url(filename))
        finally:
            github.close()
        if urls:
            for url in urls:
                print(url)
        else:
            print("No valid configurations found.")


@serve.command("start")
def start(
    host: Annotated[
        str, typer.Option(help="Bind address; use 0.0.0.0 for a container network.")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option(min=1, max=65535, help="HTTP listening port.")
    ] = 8080,
) -> None:
    """Start the HTTP service in the background and wait until listening."""
    with command_errors():
        directory = state_directory()
        state, started = start_server(directory, host, port)
        verb = "Started" if started else "Already running"
        print(f"{verb}: PID {state.pid}, {state.host}:{state.port}")
        print(f"Log: {directory / 'serve.log'}")


def token_options(
    ctx: typer.Context,
    list_: Annotated[
        bool, typer.Option("--list", "-l", help="List token names and file scopes.")
    ] = False,
    create: Annotated[
        str | None,
        typer.Option(
            "--create", "-c", help="Create a token for this configuration filename."
        ),
    ] = None,
    revoke: Annotated[
        str | None,
        typer.Option(
            "--revoke",
            "-r",
            help="Revoke this token, or all tokens for this .json filename.",
        ),
    ] = None,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            "-d",
            help="Domain; inferred when there is only one registered domain.",
        ),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Optional name for a new token.")
    ] = None,
) -> None:
    """Create, list and revoke independent subscription tokens."""
    actions = int(list_) + int(create is not None) + int(revoke is not None)
    if actions == 0 and domain is None and name is None:
        print(ctx.get_help())
        return
    if actions != 1:
        raise typer.BadParameter(
            "Choose exactly one of -l, -c FILE or -r TOKEN_OR_FILE."
        )
    if name is not None and create is None:
        raise typer.BadParameter("--name is only used with --create.")
    if list_:
        if domain is not None:
            raise typer.BadParameter("--domain is only used with --create or --revoke.")
        token_list()
    elif create is not None:
        with command_errors():
            if domain is None:
                sources = load_sources(state_directory())
                if len(sources) != 1:
                    raise ServeError(
                        "Specify --domain, or register a single domain first.", 400
                    )
                domain = sources[0].domain
        token_create(name or f"token-{uuid.uuid4().hex[:12]}", domain, create)
    else:
        assert revoke is not None
        with command_errors():
            directory = state_directory()
            with control_lock(directory):
                count = revoke_subscription_tokens(directory, revoke, domain)
            print(f"Revoked {count} subscription token(s).")


def token_create(name: str, domain: str, filename: str) -> None:
    """Generate a file-scoped token and print its secret and URL once."""
    with command_errors():
        domain = normalize_domain(domain)
        directory = state_directory()
        with control_lock(directory):
            source = next(
                (item for item in load_sources(directory) if item.domain == domain),
                None,
            )
            if source is None:
                raise ServeError(
                    "Register this domain with config serve add first.", 400
                )
            secret = create_token(directory, name, domain, filename)
        print(f"Token: {secret}")
        print(f"URL: {source.url(filename)}?token={secret}")
        print("Save the token now; only its hash is stored on the server.")


def token_list() -> None:
    """List token names and scopes without displaying their secrets."""
    with command_errors():
        entries = load_tokens(state_directory())
        if not entries:
            print("No subscription tokens.")
        for item in entries:
            print(f"{item.name}\t{item.domain}\t{item.filename}\t{item.created_at}")


@serve.command("stop")
def stop() -> None:
    """Stop this user's background configuration service."""
    with command_errors():
        stopped = stop_server(state_directory())
        print(
            "Stopped configuration service."
            if stopped
            else "Configuration service is not running."
        )
