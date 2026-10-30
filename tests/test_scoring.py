"""Standings on hand-made submissions (no judging involved)."""
from __future__ import annotations

import sqlite3

import pytest

from outpost import db, scoring

START = 1_000_000.0


@pytest.fixture
def mem(tmp_path):
    db.init(tmp_path / "s.db")
    c = db.connect(tmp_path / "s.db")
    for n, name in enumerate(["ann", "bob", "cat"], 1):
        c.execute("INSERT INTO users (id, username, password_hash, display_name, division, created_at) "
                  "VALUES (?,?,?,?,?,0)", (n, name, "x", name.title(), "A" if n < 3 else "B"))
    for pid in (1, 2):
        c.execute("INSERT INTO problems (id, slug, name, created_at) VALUES (?,?,?,0)", (pid, f"p{pid}", f"P{pid}"))
    return c


def contest(c: sqlite3.Connection, scoring_mode: str, freeze: int = 0, unfrozen: int = 0) -> sqlite3.Row:
    c.execute("DELETE FROM contests")
    c.execute("""INSERT INTO contests (id, code, name, start_at, end_at, scoring, penalty_minutes,
                 freeze_minutes, unfrozen, created_at) VALUES (1,'c','C',?,?,?,20,?,?,0)""",
              (START, START + 300 * 60, scoring_mode, freeze, unfrozen))
    for pid, label in ((1, "A"), (2, "B")):
        c.execute("INSERT OR IGNORE INTO contest_problems VALUES (1,?,?,?, '[]')", (pid, label, pid))
    return db.one(c, "SELECT * FROM contests WHERE id=1")


def sub(c, user, problem, minute, verdict, score=None, max_score=100.0, status="done"):
    c.execute("""INSERT INTO submissions (contest_id, problem_id, user_id, language, source,
                 submitted_at, status, verdict, score, max_score) VALUES (1,?,?,'cpp','',?,?,?,?,?)""",
              (problem, user, START + minute * 60 + 5, status, verdict, score, max_score))


def by_name(board):
    return {r.username: r for r in board["rows"]}


def test_icpc_penalty_and_ranking(mem):
    con = contest(mem, "icpc")
    sub(mem, 1, 1, 10, "wrong_answer")
    sub(mem, 1, 1, 15, "compilation_error")      # not a try
    sub(mem, 1, 1, 20, "accepted")               # 20 + 20 = 40
    sub(mem, 1, 1, 25, "wrong_answer")           # after AC: ignored
    sub(mem, 2, 1, 30, "accepted")               # 30
    sub(mem, 2, 2, 50, "accepted")               # 50
    sub(mem, 3, 2, 40, "accepted")               # first to solve B
    b = scoring.standings(mem, con, live=True, now=START + 400 * 60)
    rows = by_name(b)
    assert rows["bob"].rank == 1 and rows["bob"].solved == 2 and rows["bob"].penalty == 80
    assert rows["ann"].penalty == 40 and rows["ann"].cells[1].tries == 1
    assert rows["ann"].rank == rows["cat"].rank == 2  # one solved, 40 penalty each: a tie
    assert rows["cat"].cells[2].first and not rows["bob"].cells[2].first
    assert rows["ann"].cells[1].first


def test_ties_share_rank(mem):
    con = contest(mem, "icpc")
    sub(mem, 1, 1, 10, "accepted")
    sub(mem, 2, 2, 10, "accepted")
    rows = by_name(scoring.standings(mem, con, live=True))
    assert rows["ann"].rank == rows["bob"].rank == 1 and rows["cat"].rank == 3


def test_freeze_hides_late_runs(mem):
    con = contest(mem, "icpc", freeze=60)
    sub(mem, 1, 1, 10, "accepted")
    sub(mem, 2, 1, 260, "accepted")                # inside the freeze
    during = START + 270 * 60
    pub = by_name(scoring.standings(mem, con, live=False, now=during))
    assert pub["bob"].solved == 0 and pub["bob"].cells[1].pending == 1
    live = by_name(scoring.standings(mem, con, live=True, now=during))
    assert live["bob"].solved == 1
    mem.execute("UPDATE contests SET unfrozen=1")
    con = db.one(mem, "SELECT * FROM contests WHERE id=1")
    assert by_name(scoring.standings(mem, con, live=False, now=during))["bob"].solved == 1


def test_ioi_best_score(mem):
    con = contest(mem, "ioi")
    sub(mem, 1, 1, 5, "wrong_answer", 40)
    sub(mem, 1, 1, 9, "wrong_answer", 20)
    sub(mem, 1, 2, 9, "accepted", 100)
    sub(mem, 2, 1, 5, "wrong_answer", 70)
    rows = by_name(scoring.standings(mem, con, live=True))
    assert rows["ann"].score == 140 and rows["ann"].rank == 1
    assert rows["bob"].score == 70 and rows["ann"].solved == 1


def test_ioi_penalty_breaks_ties(mem):
    con = contest(mem, "ioi_penalty")
    sub(mem, 1, 1, 50, "accepted", 100)            # 50
    sub(mem, 2, 1, 10, "wrong_answer", 0)
    sub(mem, 2, 1, 20, "accepted", 100)            # 20 + 1*20 = 40
    rows = by_name(scoring.standings(mem, con, live=True))
    assert rows["bob"].penalty == 40 and rows["ann"].penalty == 50
    assert rows["bob"].rank == 1 and rows["ann"].rank == 2


def test_division_filter(mem):
    con = contest(mem, "icpc")
    sub(mem, 3, 1, 1, "accepted")
    board = scoring.standings(mem, con, live=True, division="A")
    assert [r.username for r in board["rows"]] == ["ann", "bob"]


def test_runs_outside_the_contest_dont_count(mem):
    con = contest(mem, "icpc")
    sub(mem, 1, 1, 400, "accepted")                # after the end
    assert by_name(scoring.standings(mem, con, live=True))["ann"].solved == 0


def test_csv(mem):
    con = contest(mem, "icpc")
    sub(mem, 1, 1, 10, "wrong_answer")
    sub(mem, 1, 1, 12, "accepted")
    text = scoring.to_csv(scoring.standings(mem, con, live=True))
    assert text.splitlines()[1].startswith("1,ann,Ann,A,+1 12,")


def test_resolver_data(mem):
    con = contest(mem, "icpc", freeze=60)
    sub(mem, 1, 1, 10, "accepted")                 # before the freeze: visible
    sub(mem, 2, 1, 260, "accepted")                # hidden
    sub(mem, 3, 1, 255, "wrong_answer")            # hidden
    sub(mem, 3, 1, 270, "accepted")                # hidden
    sub(mem, 3, 2, 280, "wrong_answer")            # hidden, stays unsolved
    mem.execute("UPDATE contests SET unfrozen=1")  # even after unfreezing, the resolver replays
    con = db.one(mem, "SELECT * FROM contests WHERE id=1")
    d = scoring.resolver_data(mem, con)
    rows = {r["name"]: r for r in d["rows"]}
    assert rows["Ann"]["solved"] == 1 and rows["Bob"]["solved"] == 0 and rows["Cat"]["solved"] == 0
    assert rows["Cat"]["cells"]["1"]["pending"] == 2
    bob, cat = d["reveal"][str(rows["Bob"]["id"])], d["reveal"][str(rows["Cat"]["id"])]
    assert bob["1"]["solved"] and bob["1"]["minute"] == 260 and bob["1"]["penalty"] == 260
    assert cat["1"]["solved"] and cat["1"]["tries"] == 1 and cat["1"]["penalty"] == 270 + 20
    assert not cat["2"]["solved"] and cat["2"]["tries"] == 1
    assert "1" not in d["reveal"].get(str(rows["Ann"]["id"]), {})


def test_team_contest_one_row_per_team(mem):
    con = contest(mem, "icpc")
    mem.execute("UPDATE users SET team='Pamir' WHERE username IN ('ann', 'bob')")
    mem.execute("UPDATE contests SET team_mode=1")
    con = db.one(mem, "SELECT * FROM contests WHERE id=1")
    sub(mem, 1, 1, 10, "wrong_answer")             # ann
    sub(mem, 2, 1, 20, "accepted")                 # bob: same team, same problem
    sub(mem, 1, 2, 30, "accepted")                 # ann
    sub(mem, 3, 1, 5, "accepted")                  # cat, no team: a row of her own
    rows = {r.name: r for r in scoring.standings(mem, con, live=True)["rows"]}
    assert set(rows) == {"Pamir", "Cat"}
    pamir = rows["Pamir"]
    assert pamir.team and pamir.members == ["Ann", "Bob"]
    assert pamir.solved == 2 and pamir.cells[1].tries == 1 and pamir.penalty == (20 + 20) + 30
    assert rows["Cat"].cells[1].first             # cat solved A first
    # The same people in an individual contest: three rows.
    mem.execute("UPDATE contests SET team_mode=0")
    con = db.one(mem, "SELECT * FROM contests WHERE id=1")
    assert len(scoring.standings(mem, con, live=True)["rows"]) == 3
