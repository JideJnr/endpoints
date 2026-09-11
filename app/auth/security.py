"""
Password hashing + JWT, stdlib only.

Deliberately hand-rolled instead of adding bcrypt/passlib/PyJWT as new
dependencies: this backend already leans on stdlib where it reasonably can
(see app.ai.llm using urllib instead of requests-based clients), and a
hand-rolled HS256 JWT + PBKDF2 password hash is simple enough to fully unit
test without needing anything installed beyond the standard library.

Password hashing: PBKDF2-HMAC-SHA256 with a random per-user salt (hashlib,
secrets). Not bcrypt/argon2, but a high iteration count on SHA256 is a
reasonable, dependency-free baseline for an early-stage app — revisit if/
when this handles anything more sensitive than prediction-site logins.

JWT: standard HS256 compact serialization (header.payload.signature,
base64url, no padding) so any standard JWT library/tool can still decode
and verify it if this ever needs to interop with something else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, *, salt: str | None = None) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex). Pass `salt` back in to verify
    an existing hash; omit it to generate a fresh one for a new password."""
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return derived.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, password_hash)


class TokenError(Exception):
    """Raised for any invalid/expired/malformed token — callers should treat
    this uniformly as "not authenticated", never distinguish the reason to
    a client (that's an information leak about account existence/token
    internals for no real benefit)."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def encode_token(payload: dict[str, Any], secret: str, *, expires_in_seconds: int) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + expires_in_seconds}
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(body, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64url_encode(signature))
    return ".".join(segments)


def decode_token(token: str, secret: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("Malformed token")
    header_b64, body_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_encode(expected_sig), sig_b64):
        raise TokenError("Invalid signature")
    try:
        body = json.loads(_b64url_decode(body_b64))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("Malformed token body") from exc
    exp = body.get("exp")
    if exp is not None and int(time.time()) > int(exp):
        raise TokenError("Token expired")
    return body
