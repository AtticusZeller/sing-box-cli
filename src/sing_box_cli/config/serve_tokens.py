"""Named, file-scoped subscription credentials; GitHub credentials stay separate."""

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from .serve_backend import ServeError, normalize_domain, valid_filename, write_json


@dataclass(frozen=True)
class SubscriptionToken:
    name: str
    domain: str
    filename: str
    digest: str
    created_at: str


def load_tokens(directory: Path) -> list[SubscriptionToken]:
    path = directory / "tokens.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = [SubscriptionToken(**item) for item in data["tokens"]]
        if len({item.name for item in tokens}) != len(tokens):
            raise ValueError
        for item in tokens:
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item.name)
                or normalize_domain(item.domain) != item.domain
                or not valid_filename(item.filename)
                or not re.fullmatch(r"[0-9a-f]{64}", item.digest)
                or not isinstance(item.created_at, str)
            ):
                raise ValueError
        return tokens
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ServeError):
        raise ServeError("Cannot read subscription token settings.", 503)


def create_token(directory: Path, name: str, domain: str, filename: str) -> str:
    """Return a new secret once and persist only its SHA-256 digest."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ServeError(
            "Use a token name with 1-64 letters, digits, dots, _ or -.", 400
        )
    domain = normalize_domain(domain)
    if not valid_filename(filename):
        raise ServeError("Use a root configuration filename ending in .json.", 400)
    tokens = load_tokens(directory)
    if any(item.name == name for item in tokens):
        raise ServeError(
            "Token name already exists; revoke it before reusing the name.", 409
        )
    secret = secrets.token_urlsafe(32)
    tokens.append(
        SubscriptionToken(
            name=name,
            domain=domain,
            filename=filename,
            digest=hashlib.sha256(secret.encode()).hexdigest(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    write_json(directory / "tokens.json", {"tokens": [asdict(item) for item in tokens]})
    return secret


def revoke_token(directory: Path, name: str) -> None:
    tokens = load_tokens(directory)
    remaining = [item for item in tokens if item.name != name]
    if len(remaining) == len(tokens):
        raise ServeError("Subscription token not found.", 404)
    write_json(
        directory / "tokens.json", {"tokens": [asdict(item) for item in remaining]}
    )


def revoke_subscription_tokens(
    directory: Path, target: str, domain: str | None = None
) -> int:
    """Revoke an exact secret, or every token for a configuration filename."""
    entries = load_tokens(directory)
    if domain is not None:
        domain = normalize_domain(domain)
    if valid_filename(target):
        matches = [
            item
            for item in entries
            if item.filename == target and (domain is None or item.domain == domain)
        ]
        if len({item.domain for item in matches}) > 1:
            raise ServeError(
                "This file has tokens on multiple domains; specify --domain.", 400
            )
    else:
        digest = hashlib.sha256(target.encode()).hexdigest()
        matches = [
            item
            for item in entries
            if secrets.compare_digest(item.digest, digest)
            and (domain is None or item.domain == domain)
        ]
    if not matches:
        raise ServeError("No matching subscription tokens found.", 404)
    remaining = [item for item in entries if item not in matches]
    write_json(
        directory / "tokens.json", {"tokens": [asdict(item) for item in remaining]}
    )
    return len(matches)


def authorize_subscription(
    directory: Path, domain: str, filename: str, authorization: list[str], query: str
) -> None:
    """Reject absent, conflicting, revoked or out-of-scope credentials."""
    denied = ServeError("A valid subscription token is required.", 401)
    try:
        query_tokens = parse_qs(query, keep_blank_values=True, max_num_fields=32).get(
            "token", []
        )
    except ValueError:
        raise denied
    if len(query_tokens) > 1 or len(authorization) > 1:
        raise denied
    bearer = None
    if authorization:
        parts = authorization[0].split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise denied
        bearer = parts[1]
    query_token = query_tokens[0] if query_tokens else None
    if bearer is not None and query_token is not None and bearer != query_token:
        raise denied
    secret = bearer if bearer is not None else query_token
    if not secret or len(secret) > 512 or any(char.isspace() for char in secret):
        raise denied
    digest = hashlib.sha256(secret.encode()).hexdigest()
    if not any(
        item.domain == domain
        and item.filename == filename
        and secrets.compare_digest(item.digest, digest)
        for item in load_tokens(directory)
    ):
        raise denied
