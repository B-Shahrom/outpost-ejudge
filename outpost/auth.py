"""Passwords and sessions.

Contestants get one live session: signing in again (another laptop, a friend's
phone) ends the previous one, so an account can't be shared mid-contest. The
session cookie carries the user id and a token that must match the one stored
on the user row.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3

from . import db

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read off a paper slip


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(got, base64.b64decode(digest))


def generate_password(length: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def start_session(conn: sqlite3.Connection, user: sqlite3.Row, ip: str | None) -> str:
    token = secrets.token_urlsafe(24)
    if user["is_admin"]:
        # Admins may be signed in from several places; keep an existing token.
        token = user["session_token"] or token
    conn.execute("UPDATE users SET session_token=?, session_ip=? WHERE id=?", (token, ip, user["id"]))
    return token


def session_user(conn: sqlite3.Connection, session: dict) -> sqlite3.Row | None:
    uid, token = session.get("uid"), session.get("token")
    if not uid or not token:
        return None
    user = db.one(conn, "SELECT * FROM users WHERE id=?", (uid,))
    if user is None or not user["session_token"] or not hmac.compare_digest(user["session_token"], token):
        return None
    return user


def create_user(conn: sqlite3.Connection, username: str, password: str, *, display_name: str = "",
                division: str = "", is_admin: bool = False) -> int:
    return conn.execute(
        "INSERT INTO users (username, password_hash, display_name, division, is_admin, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (username, hash_password(password), display_name, division, int(is_admin), db.now()),
    ).lastrowid
