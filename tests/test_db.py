"""Upgrading: a database from an older Outpost gets the new columns."""
import sqlite3

from outpost import db


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    # The 0.1 tables, before teams and judge leases.
    old.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '', division TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL
            DEFAULT 0, session_token TEXT, session_ip TEXT, created_at REAL NOT NULL);
        CREATE TABLE contests (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            start_at REAL NOT NULL, end_at REAL NOT NULL, scoring TEXT NOT NULL DEFAULT 'icpc',
            penalty_minutes INTEGER NOT NULL DEFAULT 20, freeze_minutes INTEGER NOT NULL DEFAULT 0,
            divisions_json TEXT NOT NULL DEFAULT '[]', visible INTEGER NOT NULL DEFAULT 1,
            public_standings INTEGER NOT NULL DEFAULT 1, unfrozen INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL);
        INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 0);
    """)
    old.close()
    db.init(path)
    db.init(path)  # running it again changes nothing
    c = db.connect(path)
    assert db.one(c, "SELECT team FROM users WHERE username='old'")["team"] == ""
    assert "team_mode" in {r["name"] for r in c.execute("PRAGMA table_info(contests)")}
    assert {"judged_by", "lease_until"} <= {r["name"] for r in c.execute("PRAGMA table_info(submissions)")}
