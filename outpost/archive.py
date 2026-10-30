"""Contest archives: a whole contest in one zip, to move it to another laptop
or keep it.

    archive.json            contest settings, its problems' rows, users,
                            submissions, clarifications
    problems/<slug>/...     each problem's directory (tests, statements, and the
                            checker/interactor sources in src/ — binaries are
                            rebuilt on import, they may not run elsewhere)

Importing never overwrites anything: a problem whose short name exists is
kept as it is (the archive's copy is skipped), an existing user keeps their
password, and a contest code that is taken needs a new one. Submissions come
in judged, so nothing has to run again.

``include_people=False`` leaves users, submissions and clarifications out: the
problem set and the contest's settings only, to share.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import db, polygon
from .config import Settings

FORMAT = "outpost-contest"
VERSION = 1

PROBLEM_FIELDS = ("slug", "name", "time_limit_ms", "memory_limit_mb", "interactive", "checker",
                  "tests_json", "groups_json", "statements_json", "status", "import_log", "main_max_ms")
CONTEST_FIELDS = ("code", "name", "start_at", "end_at", "scoring", "penalty_minutes", "freeze_minutes",
                  "divisions_json", "visible", "public_standings", "unfrozen", "team_mode")
FIELD_DEFAULTS = {"team_mode": 0, "team": ""}  # fields older archives don't have
SUBMISSION_FIELDS = ("language", "source", "submitted_at", "status", "verdict", "score", "max_score",
                     "time_ms", "memory_kb", "failed_test", "compile_output", "test_results_json",
                     "group_results_json", "judged_at")
SKIP_IN_PROBLEM_DIR = {"check", "interactor"}  # binaries: rebuilt from src/ on import


class ArchiveError(Exception):
    pass


def export_contest(conn: sqlite3.Connection, settings: Settings, code: str, out: Path,
                   include_people: bool = True) -> dict:
    contest = db.one(conn, "SELECT * FROM contests WHERE code=?", (code,))
    if contest is None:
        raise ArchiveError(f"no contest {code!r}")
    problems = db.all_(conn, """SELECT p.*, cp.label, cp.position, cp.divisions_json AS cp_divisions
                                FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id
                                WHERE cp.contest_id=? ORDER BY cp.position""", (contest["id"],))
    data = {
        "format": FORMAT, "version": VERSION,
        "contest": {k: contest[k] for k in CONTEST_FIELDS},
        "problems": [{**{k: p[k] for k in PROBLEM_FIELDS}, "label": p["label"], "position": p["position"],
                      "divisions_json": p["cp_divisions"]} for p in problems],
        "users": [], "submissions": [], "clarifications": [],
    }
    if include_people:
        subs = db.all_(conn, """SELECT s.*, u.username, p.slug FROM submissions s
                                JOIN users u ON u.id=s.user_id JOIN problems p ON p.id=s.problem_id
                                WHERE s.contest_id=? ORDER BY s.id""", (contest["id"],))
        divisions = db.loads(contest["divisions_json"], [])
        people = {u["username"]: u for u in db.all_(conn, "SELECT * FROM users WHERE is_admin=0")
                  if not divisions or u["division"] in divisions}
        for s in subs:  # anyone who submitted, even if their division changed since
            if s["username"] not in people:
                people[s["username"]] = db.one(conn, "SELECT * FROM users WHERE username=?", (s["username"],))
        data["users"] = [{k: u[k] for k in ("username", "password_hash", "display_name", "division",
                                            "is_admin", "team")} for u in people.values()]
        data["submissions"] = [{**{k: s[k] for k in SUBMISSION_FIELDS}, "username": s["username"],
                                "slug": s["slug"]} for s in subs]
        clars = db.all_(conn, """SELECT cl.*, u.username, p.slug FROM clarifications cl
                                 LEFT JOIN users u ON u.id=cl.user_id LEFT JOIN problems p ON p.id=cl.problem_id
                                 WHERE cl.contest_id=? ORDER BY cl.id""", (contest["id"],))
        data["clarifications"] = [{k: c[k] for k in ("username", "slug", "question", "answer", "public",
                                                     "created_at", "answered_at")} for c in clars]

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("archive.json", json.dumps(data, ensure_ascii=False, indent=1))
        for p in problems:
            pdir = settings.problems_dir / p["slug"]
            has_src = (pdir / "src").is_dir()
            for f in sorted(pdir.rglob("*")):
                if f.is_file() and not (has_src and f.name in SKIP_IN_PROBLEM_DIR and f.parent == pdir):
                    zf.write(f, f"problems/{p['slug']}/{f.relative_to(pdir)}")
    return {"problems": len(problems), "users": len(data["users"]), "submissions": len(data["submissions"])}


def import_contest(conn: sqlite3.Connection, settings: Settings, archive: Path, new_code: str | None = None,
                   log: Callable[[str], None] = print) -> str:
    """Import ``archive``; returns the contest code it was created under."""
    settings.ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="archive-", dir=settings.uploads_dir))
    try:
        try:
            polygon.safe_extract(archive, work)
            data = json.loads((work / "archive.json").read_text(encoding="utf-8"))
        except (zipfile.BadZipFile, FileNotFoundError, ValueError, polygon.ImportError_) as e:
            raise ArchiveError(f"not an Outpost contest archive ({e})") from None
        if data.get("format") != FORMAT or int(data.get("version", 0)) > VERSION:
            raise ArchiveError("not an Outpost contest archive (or one from a newer Outpost)")
        contest = data["contest"]
        code = (new_code or contest["code"]).strip()
        if db.one(conn, "SELECT 1 FROM contests WHERE code=?", (code,)):
            raise ArchiveError(f"a contest with code {code!r} exists; import it under another code")

        copied: list[Path] = []
        conn.execute("BEGIN")
        try:
            code = _import_rows(conn, settings, work, data, code, copied, log)
            conn.execute("COMMIT")
            return code
        except BaseException:
            conn.execute("ROLLBACK")  # all or nothing: no half-imported contest
            for d in copied:
                shutil.rmtree(d, ignore_errors=True)
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _import_rows(conn, settings, work: Path, data: dict, code: str, copied: list[Path], log) -> str:
    contest = data["contest"]
    problem_ids: dict[str, int] = {}
    for p in data["problems"]:
        row = db.one(conn, "SELECT id FROM problems WHERE slug=?", (p["slug"],))
        if row:
            problem_ids[p["slug"]] = row["id"]
            log(f"problem {p['slug']}: already here, kept as it is")
            continue
        src_dir = work / "problems" / p["slug"]
        dest = settings.problems_dir / p["slug"]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src_dir, dest)
        copied.append(dest)
        polygon.rebuild_binaries(dest, log)
        for name in ("check", "interactor"):
            if (dest / name).exists():
                (dest / name).chmod(0o700)
        cols = list(PROBLEM_FIELDS)
        problem_ids[p["slug"]] = conn.execute(
            f"INSERT INTO problems ({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
            [p[c] for c in cols] + [db.now()]).lastrowid
        log(f"problem {p['slug']}: imported")

    cols = list(CONTEST_FIELDS)
    values = {**{c: contest.get(c, FIELD_DEFAULTS.get(c)) for c in cols}, "code": code}
    cid = conn.execute(f"INSERT INTO contests ({', '.join(cols)}, created_at) VALUES "
                       f"({', '.join('?' * len(cols))}, ?)", [values[c] for c in cols] + [db.now()]).lastrowid
    for p in data["problems"]:
        conn.execute("INSERT INTO contest_problems VALUES (?,?,?,?,?)",
                     (cid, problem_ids[p["slug"]], p["label"], p["position"], p["divisions_json"]))

    user_ids: dict[str, int] = {}
    created = 0
    for u in data.get("users", []):
        row = db.one(conn, "SELECT id FROM users WHERE username=?", (u["username"],))
        if row:
            user_ids[u["username"]] = row["id"]
            continue
        user_ids[u["username"]] = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, division, is_admin, team, created_at) "
            "VALUES (?,?,?,?,?,?,?)", (u["username"], u["password_hash"], u["display_name"], u["division"],
                                       u["is_admin"], u.get("team", ""), db.now())).lastrowid
        created += 1
    for s in data.get("submissions", []):
        cols = list(SUBMISSION_FIELDS)
        conn.execute(f"INSERT INTO submissions (contest_id, problem_id, user_id, {', '.join(cols)}) "
                     f"VALUES (?,?,?,{', '.join('?' * len(cols))})",
                     [cid, problem_ids[s["slug"]], user_ids[s["username"]]]
                     + [s[c] if not (c == "status" and s[c] != "done") else "pending" for c in cols])
    for c in data.get("clarifications", []):
        conn.execute("INSERT INTO clarifications (contest_id, user_id, problem_id, question, answer, public, "
                     "created_at, answered_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, user_ids.get(c["username"]) if c["username"] else None,
                      problem_ids.get(c["slug"]) if c["slug"] else None, c["question"], c["answer"],
                      c["public"], c["created_at"], c["answered_at"]))
    log(f"contest {code}: {len(data['problems'])} problem(s), {created} new user(s) "
        f"({len(data.get('users', [])) - created} already here), {len(data.get('submissions', []))} "
        f"submission(s), {len(data.get('clarifications', []))} question(s)")
    return code
