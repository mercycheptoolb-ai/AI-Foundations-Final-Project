"""Health-worker accounts: scrypt password hashes, server-side sessions, and a login lockout."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from .store import connect

SESSION_COOKIE = "kukua_session"
SESSION_HOURS = 12
MAX_FAILED = 5          # failed logins before the account is locked
LOCK_MINUTES = 10
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    facility TEXT NOT NULL,
    salt BLOB NOT NULL,
    pw_hash BLOB NOT NULL,
    failed INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Demo account for graders and the demo video (fictional; documented in README.md).
DEMO_USER = {"username": "demo", "full_name": "Demo CHP (Wanjiku)", "facility": "Kibera Community Unit",
             "password": "kukua-demo"}


class AuthError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _db():
    con = connect()
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        _insert(con, DEMO_USER["username"], DEMO_USER["full_name"], DEMO_USER["facility"], DEMO_USER["password"])
    return con


def _insert(con, username: str, full_name: str, facility: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    con.execute("INSERT INTO users (username, full_name, facility, salt, pw_hash, created_at) VALUES (?,?,?,?,?,?)",
                (username, full_name, facility, salt, _hash(password, salt), _now().isoformat()))
    con.commit()


def public(row) -> dict:
    return {"username": row["username"], "full_name": row["full_name"], "facility": row["facility"]}


def register(username: str, full_name: str, facility: str, password: str) -> dict:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3–30 characters: letters, numbers, dot, dash or underscore.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if not full_name.strip() or not facility.strip():
        raise AuthError("Enter your name and your health facility or community unit.")
    with _db() as con:
        if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise AuthError("That username is taken.")
        _insert(con, username, full_name.strip()[:80], facility.strip()[:80], password)
        return public(con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def login(username: str, password: str) -> tuple[str, dict]:
    """Returns (session token, user). Same error for unknown user and wrong password."""
    username = username.strip().lower()
    with _db() as con:
        row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > _now():
            raise AuthError(f"Too many failed attempts. Try again after {LOCK_MINUTES} minutes.")
        if not row or not hmac.compare_digest(_hash(password, row["salt"]), row["pw_hash"]):
            if row:
                failed = row["failed"] + 1
                locked = (_now() + timedelta(minutes=LOCK_MINUTES)).isoformat() if failed >= MAX_FAILED else None
                con.execute("UPDATE users SET failed = ?, locked_until = ? WHERE username = ?",
                            (0 if locked else failed, locked, username))
                con.commit()  # keep the failure count: leaving the with-block by an exception would roll it back
            raise AuthError("Wrong username or password.")
        con.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE username = ?", (username,))
        token = secrets.token_urlsafe(32)
        con.execute("INSERT INTO sessions (token_hash, username, expires_at) VALUES (?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(), username,
                     (_now() + timedelta(hours=SESSION_HOURS)).isoformat()))
        return token, public(row)


def user_for(token: str | None) -> dict | None:
    if not token:
        return None
    with _db() as con:
        row = con.execute(
            "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.username = s.username WHERE s.token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) < _now():
        return None
    return public(row)


def logout(token: str | None) -> None:
    if token:
        with _db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
