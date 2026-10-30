"""Standings for the three electicode scoring modes.

* **ICPC** — rank by problems solved, then penalty: for each solved problem,
  minutes from the start to the first accepted run plus ``penalty_minutes``
  for every rejected run before it. Compilation errors and judge errors don't
  count as tries.
* **IOI** — a problem's score is the best score of any of its runs; rank by the
  total. Equal totals share a place.
* **IOI with penalty** — IOI totals, ties broken by penalty: for each problem
  with a positive score, minutes to the run that first reached the best score
  plus ``penalty_minutes`` per run before it.

**Freeze.** In the last ``freeze_minutes`` of an ICPC/IOI contest, runs by
everyone show as pending ("?") on the public board until the admin unfreezes.
Admins always see the live board.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db

NOT_TRIES = {"compilation_error", "judge_error"}


@dataclass
class Cell:
    tries: int = 0              # rejected runs before the counted one
    runs: int = 0               # judged runs so far (IOI)
    pending: int = 0            # runs hidden by the freeze or still judging
    solved: bool = False
    first: bool = False         # first to solve it in the contest
    minute: int | None = None   # minute of the counted run
    score: float | None = None  # IOI best score (None = no runs)
    max_score: float | None = None
    penalty: int = 0


@dataclass
class Row:
    user_id: int           # a team row: its first member's id (unique per row all the same)
    username: str          # a team row: the team's name
    name: str
    division: str
    members: list[str] = field(default_factory=list)  # display names (team rows)
    team: bool = False
    key: str = ""          # "t:<team>" or "u:<user id>"
    seats: list[str] = field(default_factory=list)
    cells: dict[int, Cell] = field(default_factory=dict)
    solved: int = 0
    score: float = 0.0
    penalty: int = 0
    rank: int = 0
    last_minute: int = 0


def is_frozen(contest: sqlite3.Row, now: float) -> bool:
    if contest["unfrozen"] or not contest["freeze_minutes"]:
        return False
    return now >= contest["end_at"] - contest["freeze_minutes"] * 60


def standings(conn: sqlite3.Connection, contest: sqlite3.Row, *, live: bool,
              division: str | None = None, now: float | None = None,
              force_frozen: bool = False) -> dict:
    """Compute the board. ``live`` (admins) ignores the freeze; ``force_frozen``
    shows the frozen board even after the contest was unfrozen (the resolver)."""
    now = db.now() if now is None else now
    frozen = force_frozen or (is_frozen(contest, now) and not live)
    freeze_at = contest["end_at"] - contest["freeze_minutes"] * 60
    divisions = db.loads(contest["divisions_json"], [])
    problems = db.all_(conn, """
        SELECT p.id, p.slug, p.name, cp.label FROM contest_problems cp
        JOIN problems p ON p.id = cp.problem_id
        WHERE cp.contest_id=? ORDER BY cp.position""", (contest["id"],))

    users_sql = "SELECT id, username, display_name, division, team, seat FROM users WHERE is_admin=0"
    params: list = []
    if divisions:
        users_sql += f" AND division IN ({','.join('?' * len(divisions))})"
        params += divisions
    if division:
        users_sql += " AND division=?"
        params.append(division)
    # One row per contestant, or per team in a team contest (a member without
    # a team still gets a row of their own).
    team_mode = bool(contest["team_mode"])
    rows: dict[str, Row] = {}
    owner: dict[int, str] = {}  # user id -> row key
    for u in db.all_(conn, users_sql + " ORDER BY id", params):
        shown = u["display_name"] or u["username"]
        key = f"t:{u['team']}" if team_mode and u["team"] else f"u:{u['id']}"
        if key not in rows:
            rows[key] = (Row(u["id"], u["team"], u["team"], u["division"], team=True) if key.startswith("t:")
                         else Row(u["id"], u["username"], shown, u["division"]))
        rows[key].key = key
        rows[key].members.append(shown)
        if u["seat"]:
            rows[key].seats.append(u["seat"])
        owner[u["id"]] = key

    subs = db.all_(conn, """
        SELECT id, user_id, problem_id, submitted_at, status, verdict, score, max_score
        FROM submissions WHERE contest_id=? AND submitted_at BETWEEN ? AND ?
        ORDER BY submitted_at, id""", (contest["id"], contest["start_at"], contest["end_at"]))

    scoring = contest["scoring"]
    pen = contest["penalty_minutes"]
    first_solve: dict[int, tuple[float, str]] = {}
    for s in subs:
        row = rows.get(owner.get(s["user_id"], ""))
        if row is None:
            continue
        cell = row.cells.setdefault(s["problem_id"], Cell())
        minute = int((s["submitted_at"] - contest["start_at"]) // 60)
        hidden = frozen and s["submitted_at"] >= freeze_at
        if s["status"] != "done" or hidden:
            if not cell.solved:
                cell.pending += 1
            continue
        if s["verdict"] in NOT_TRIES:
            continue
        if scoring == "icpc":
            if cell.solved:
                continue
            if s["verdict"] == "accepted":
                cell.solved, cell.minute = True, minute
                cell.penalty = minute + cell.tries * pen
                best = first_solve.get(s["problem_id"])
                if best is None or s["submitted_at"] < best[0]:
                    first_solve[s["problem_id"]] = (s["submitted_at"], owner[s["user_id"]])
            else:
                cell.tries += 1
        else:
            score = s["score"] or 0.0
            cell.max_score = s["max_score"]
            if cell.score is None or score > cell.score:
                cell.score, cell.minute, cell.tries = score, minute, cell.runs
                cell.penalty = minute + cell.runs * pen
                cell.solved = s["max_score"] is not None and score >= s["max_score"]
            cell.runs += 1

    for pid, (_, key) in first_solve.items():
        rows[key].cells[pid].first = True

    for row in rows.values():
        for cell in row.cells.values():
            if scoring == "icpc":
                if cell.solved:
                    row.solved += 1
                    row.penalty += cell.penalty
                    row.last_minute = max(row.last_minute, cell.minute or 0)
            else:
                row.score += cell.score or 0.0
                row.solved += int(cell.solved)
                if (cell.score or 0) > 0:
                    row.penalty += cell.penalty

    if scoring == "icpc":
        key = lambda r: (-r.solved, r.penalty)
    elif scoring == "ioi_penalty":
        key = lambda r: (-round(r.score, 6), r.penalty)
    else:
        key = lambda r: (-round(r.score, 6),)
    ordered = sorted(rows.values(), key=lambda r: (key(r), r.name.lower()))
    for n, row in enumerate(ordered):
        if n and key(row) == key(ordered[n - 1]):
            row.rank = ordered[n - 1].rank
        else:
            row.rank = n + 1

    return {"problems": problems, "rows": ordered, "frozen": frozen,
            "scoring": scoring, "divisions": divisions, "teams": team_mode}


def resolver_data(conn: sqlite3.Connection, contest: sqlite3.Row) -> dict:
    """The frozen ICPC board, plus the final result of every cell the freeze hid.

    The resolver page replays them: lowest-ranked team with a hidden cell
    first, its leftmost hidden problem, re-rank, repeat.
    """
    frozen = standings(conn, contest, live=False, force_frozen=True)
    final = standings(conn, contest, live=True)
    final_cells = {r.user_id: r.cells for r in final["rows"]}

    def cell(c: Cell | None) -> dict:
        if c is None:
            return {"solved": False, "tries": 0, "pending": 0, "minute": None, "penalty": 0, "first": False}
        return {"solved": c.solved, "tries": c.tries, "pending": c.pending, "minute": c.minute,
                "penalty": c.penalty, "first": c.first}

    rows, reveal = [], {}
    for r in frozen["rows"]:
        rows.append({"id": r.user_id, "name": r.name, "division": r.division, "solved": r.solved,
                     "penalty": r.penalty, "cells": {str(p["id"]): cell(r.cells.get(p["id"]))
                                                     for p in frozen["problems"]}})
        for pid, c in r.cells.items():
            if c.pending:
                reveal.setdefault(str(r.user_id), {})[str(pid)] = cell(final_cells.get(r.user_id, {}).get(pid))
    return {"problems": [{"id": str(p["id"]), "label": p["label"], "name": p["name"]} for p in frozen["problems"]],
            "rows": rows, "reveal": reveal, "penalty_minutes": contest["penalty_minutes"]}


def to_csv(board: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    labels = [p["label"] for p in board["problems"]]
    icpc = board["scoring"] == "icpc"
    w.writerow(["rank", "username", "name", "division", *labels,
                "solved" if icpc else "score", "penalty"])
    for r in board["rows"]:
        cells = []
        for p in board["problems"]:
            c = r.cells.get(p["id"])
            if c is None:
                cells.append("")
            elif icpc:
                cells.append(f"+{c.tries or ''} {c.minute}" if c.solved else
                             (f"-{c.tries}" if c.tries else ""))
            else:
                cells.append("" if c.score is None else f"{c.score:g}")
        w.writerow([r.rank, r.username, r.name, r.division, *cells,
                    r.solved if icpc else f"{r.score:g}", r.penalty])
    return buf.getvalue()
