"""Browser sessions for the dashboard. The CLI keeps using the admin bearer token."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any


def hash_password(password: str, *, rounds: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, rounds_text, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds_text),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


class SessionAuth:
    def __init__(
        self,
        *,
        username: str,
        password_hash: str | None,
        secret: bytes,
        ttl_seconds: int,
        limit: int,
        window_seconds: int = 300,
    ) -> None:
        self.username = username
        self.password_hash = password_hash or None
        self.secret = secret
        self.ttl_seconds = ttl_seconds
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, admin_token: str) -> SessionAuth:
        secret_text = os.environ.get("DASHBOARD_SESSION_SECRET")
        if not secret_text:
            secret_text = hashlib.sha256(f"{admin_token}:dashboard-session".encode("utf-8")).hexdigest()
        return cls(
            username=os.environ.get("DASHBOARD_ADMIN_USER", "admin"),
            password_hash=os.environ.get("DASHBOARD_ADMIN_PASSWORD_HASH") or None,
            secret=secret_text.encode("utf-8"),
            ttl_seconds=int(os.environ.get("DASHBOARD_SESSION_SECONDS", "43200")),
            limit=int(os.environ.get("DASHBOARD_LOGIN_LIMIT", "8")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

    def too_many(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [item for item in self._failures.get(ip, []) if now - item < self.window_seconds]
            self._failures[ip] = recent
            return len(recent) >= self.limit

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            recent = [item for item in self._failures.get(ip, []) if now - item < self.window_seconds]
            recent.append(now)
            self._failures[ip] = recent

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def check_password(self, username: str, password: str) -> bool:
        if not self.password_hash:
            return False
        if not hmac.compare_digest(username, self.username):
            return False
        return verify_password(password, self.password_hash)

    def issue(self, username: str) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        payload = {"u": username, "exp": int(time.time()) + self.ttl_seconds, "csrf": csrf}
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self.secret, raw.encode("ascii"), hashlib.sha256).digest()
        return f"{raw}.{_b64(signature)}", csrf

    def parse(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        raw, signature = token.split(".", 1)
        expected = hmac.new(self.secret, raw.encode("ascii"), hashlib.sha256).digest()
        try:
            presented = _b64decode(signature)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(presented, expected):
            return None
        try:
            payload = json.loads(_b64decode(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if payload.get("u") != self.username or not payload.get("csrf"):
            return None
        return payload
