"""SQLite storage. One file, WAL mode, plain SQL — no ORM.

The web server and every judge worker open their own connection; WAL lets
readers (standings, status pages) run while a worker writes a verdict.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    division      TEXT NOT NULL DEFAULT '',
    team          TEXT NOT NULL DEFAULT '',         -- team contests: one standings row per team
    is_admin      INTEGER NOT NULL DEFAULT 0,
    session_token TEXT,
    session_ip    TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    time_limit_ms   INTEGER NOT NULL DEFAULT 1000,
    memory_limit_mb INTEGER NOT NULL DEFAULT 256,
    interactive     INTEGER NOT NULL DEFAULT 0,
    checker         TEXT NOT NULL DEFAULT 'builtin',   -- 'builtin' | 'testlib'
    tests_json      TEXT NOT NULL DEFAULT '[]',        -- [{index, group, points, sample}]
    groups_json     TEXT NOT NULL DEFAULT '[]',        -- [{name, points, policy, dependencies}]
    statements_json TEXT NOT NULL DEFAULT '{}',        -- {lang: {name, legend, ...}}
    status          TEXT NOT NULL DEFAULT 'importing', -- importing | ready | error
    import_log      TEXT NOT NULL DEFAULT '',
    main_max_ms     INTEGER,                           -- slowest test of the main solution here
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contests (
    id               INTEGER PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    start_at         REAL NOT NULL,
    end_at           REAL NOT NULL,
    scoring          TEXT NOT NULL DEFAULT 'icpc',      -- icpc | ioi | ioi_penalty
    penalty_minutes  INTEGER NOT NULL DEFAULT 20,
    freeze_minutes   INTEGER NOT NULL DEFAULT 0,
    divisions_json   TEXT NOT NULL DEFAULT '[]',        -- [] = open to everyone
    visible          INTEGER NOT NULL DEFAULT 1,
    public_standings INTEGER NOT NULL DEFAULT 1,
    unfrozen         INTEGER NOT NULL DEFAULT 0,
    team_mode        INTEGER NOT NULL DEFAULT 0,        -- standings by team, not by user
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contest_problems (
    contest_id     INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    problem_id     INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    label          TEXT NOT NULL,
    position       INTEGER NOT NULL,
    divisions_json TEXT NOT NULL DEFAULT '[]',          -- [] = every division in the contest
    PRIMARY KEY (contest_id, problem_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id                 INTEGER PRIMARY KEY,
    contest_id         INTEGER REFERENCES contests(id) ON DELETE CASCADE,
    problem_id         INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    language           TEXT NOT NULL,
    source             TEXT NOT NULL,
    submitted_at       REAL NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | judging | done
    verdict            TEXT,
    score              REAL,
    max_score          REAL,
    time_ms            INTEGER,
    memory_kb          INTEGER,
    failed_test        INTEGER,
    compile_output     TEXT,
    test_results_json  TEXT,
    group_results_json TEXT,
    judged_at          REAL,
    judged_by          TEXT,                         -- which judge holds or held it
    lease_until        REAL                          -- back to the queue if not done by then
);
CREATE INDEX IF NOT EXISTS submissions_queue ON submissions(status, id);
CREATE INDEX IF NOT EXISTS submissions_contest ON submissions(contest_id, user_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clarifications (
    id          INTEGER PRIMARY KEY,
    contest_id  INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,   -- NULL = announcement
    problem_id  INTEGER REFERENCES problems(id) ON DELETE SET NULL,
    question    TEXT NOT NULL DEFAULT '',
    answer      TEXT,
    public      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    answered_at REAL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Columns added after a table was first released: (table, column, declaration).
# CREATE TABLE IF NOT EXISTS leaves an older database's tables as they were.
MIGRATIONS = [
    ("submissions", "judged_by", "TEXT"),
    ("submissions", "lease_until", "REAL"),
    ("users", "team", "TEXT NOT NULL DEFAULT ''"),
    ("contests", "team_mode", "INTEGER NOT NULL DEFAULT 0"),
]


def init(path: Path) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.close()


def now() -> float:
    return time.time()


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = one(conn, "SELECT value FROM app_settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    if value:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    else:
        conn.execute("DELETE FROM app_settings WHERE key=?", (key,))


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    return json.loads(text)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
