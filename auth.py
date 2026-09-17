"""Password hashing and cookie sessions."""
import hashlib
import secrets
import sqlite3
from typing import Annotated

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Response

from db import get_db

SESSION_COOKIE = "hsa_session"
SESSION_DAYS = 7
BCRYPT_ROUNDS = 12

# Checked when the email is unknown, so that case takes as long as a wrong password.
_DUMMY_HASH = bcrypt.hashpw(b"no-such-account", bcrypt.gensalt(BCRYPT_ROUNDS))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> int | None:
    row = conn.execute("SELECT id, password_hash FROM accounts WHERE email = ?", (email,)).fetchone()
    stored = row["password_hash"].encode() if row else _DUMMY_HASH
    matches = bcrypt.checkpw(password.encode(), stored)
    return row["id"] if row and matches else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(conn: sqlite3.Connection, response: Response, account_id: int) -> None:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
    conn.execute(
        "INSERT INTO sessions (token_hash, account_id, expires_at) VALUES (?, ?, datetime('now', ?))",
        (_token_hash(token), account_id, f"+{SESSION_DAYS} days"),
    )
    # A real deployment adds secure=True; it is left off so the cookie works on http://localhost.
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax"
    )


def end_session(conn: sqlite3.Connection, response: Response, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax")


def current_account_id(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> int:
    """FastAPI dependency: the logged-in account's id, or 401."""
    if token:
        row = conn.execute(
            "SELECT account_id FROM sessions WHERE token_hash = ? AND expires_at > datetime('now')",
            (_token_hash(token),),
        ).fetchone()
        if row:
            return row["account_id"]
    raise HTTPException(status_code=401, detail="Not logged in.")
