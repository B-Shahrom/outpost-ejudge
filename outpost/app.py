"""The web server: contestant pages, admin pages, and a little JSON for polling.

Server-rendered HTML (Jinja2) with a few lines of plain JS — nothing is loaded
from the internet, because the room may not have any.
"""

from __future__ import annotations

import csv
import io
import json
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import segno
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, languages, scoring
from .config import Settings
from .judge import VERDICTS

HERE = Path(__file__).parent
MAX_SOURCE_BYTES = 128 * 1024
SUBMIT_COOLDOWN_SECONDS = 5

VERDICT_LABELS = {
    "accepted": "Accepted", "wrong_answer": "Wrong answer",
    "time_limit_exceeded": "Time limit exceeded", "memory_limit_exceeded": "Memory limit exceeded",
    "runtime_error": "Runtime error", "compilation_error": "Compilation error",
    "judge_error": "Judge error", "skipped": "Skipped",
}
SCORING_LABELS = {"icpc": "ICPC", "ioi": "IOI", "ioi_penalty": "IOI with penalty"}


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(settings: Settings, pool=None, mdns_host: str | None = None) -> FastAPI:
    settings.ensure_dirs()
    db.init(settings.db_path)
    app = FastAPI(title="Outpost", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key(),
                       session_cookie="outpost", same_site="lax", max_age=7 * 24 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters["clock"] = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else ""
    env.filters["datetime"] = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
    env.filters["dtlocal"] = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M") if t else ""
    env.filters["verdict"] = lambda v: VERDICT_LABELS.get(v or "", v or "")
    env.filters["fromjson"] = lambda s: json.loads(s) if s else None
    env.filters["num"] = lambda x: "" if x is None else f"{x:g}"
    env.globals["SCORING"] = SCORING_LABELS
    env.globals["LANGS"] = languages.LANGUAGES
    app.state.settings = settings
    app.state.pool = pool
    app.state.mdns_host = mdns_host  # e.g. "outpost.local", when announced
    app.state.imports: dict[str, dict] = {}
    # New accounts with their plain passwords, kept in memory only (never in
    # the database) until the admin has printed the slips.
    app.state.batches: dict[str, list[dict]] = {}

    # -- plumbing ------------------------------------------------------------
    def conn() -> sqlite3.Connection:
        return db.connect(settings.db_path)

    @app.exception_handler(Redirect)
    async def _redirect(_req: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    def user_of(request: Request, c: sqlite3.Connection) -> sqlite3.Row | None:
        return auth.session_user(c, request.session)

    def need_user(request: Request, c: sqlite3.Connection) -> sqlite3.Row:
        u = user_of(request, c)
        if u is None:
            request.session.clear()
            raise Redirect("/login")
        return u

    def need_admin(request: Request, c: sqlite3.Connection) -> sqlite3.Row:
        u = need_user(request, c)
        if not u["is_admin"]:
            raise HTTPException(403, "admins only")
        return u

    def render(request: Request, name: str, c: sqlite3.Connection, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("user", user_of(request, c))
        ctx.setdefault("now", time.time())
        ctx["flash"] = request.session.pop("flash", None)
        return templates.TemplateResponse(request, name, ctx)

    def flash(request: Request, msg: str) -> None:
        request.session["flash"] = msg

    def contest_by_code(c: sqlite3.Connection, code: str, user: sqlite3.Row) -> sqlite3.Row:
        contest = db.one(c, "SELECT * FROM contests WHERE code=?", (code,))
        if contest is None or not can_see(contest, user):
            raise HTTPException(404, "no such contest")
        return contest

    def can_see(contest: sqlite3.Row, user: sqlite3.Row) -> bool:
        if user["is_admin"]:
            return True
        divisions = db.loads(contest["divisions_json"], [])
        return bool(contest["visible"]) and (not divisions or user["division"] in divisions)

    def contest_problems(c, contest, user) -> list[sqlite3.Row]:
        rows = db.all_(c, """
            SELECT p.*, cp.label, cp.position, cp.divisions_json AS cp_divisions
            FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id
            WHERE cp.contest_id=? ORDER BY cp.position""", (contest["id"],))
        if user["is_admin"]:
            return rows
        return [r for r in rows if not db.loads(r["cp_divisions"], [])
                or user["division"] in db.loads(r["cp_divisions"], [])]

    def started(contest, user) -> bool:
        return user["is_admin"] or time.time() >= contest["start_at"]

    # -- sign in -----------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        c = conn()
        return render(request, "login.html", c, user=None)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        c = conn()
        u = db.one(c, "SELECT * FROM users WHERE username=?", (username.strip(),))
        if u is None or not auth.check_password(password, u["password_hash"]):
            flash(request, "Wrong username or password.")
            raise Redirect("/login")
        ip = request.client.host if request.client else None
        request.session.clear()
        request.session.update(uid=u["id"], token=auth.start_session(c, u, ip))
        raise Redirect("/admin" if u["is_admin"] else "/")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        raise Redirect("/login")

    # -- contestant ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        c = conn()
        u = need_user(request, c)
        contests = [x for x in db.all_(c, "SELECT * FROM contests ORDER BY start_at DESC")
                    if can_see(x, u)]
        running = [x for x in contests if x["start_at"] <= time.time() <= x["end_at"]]
        if not u["is_admin"] and len(running) == 1:
            raise Redirect(f"/c/{running[0]['code']}")
        return render(request, "home.html", c, contests=contests)

    @app.get("/c/{code}", response_class=HTMLResponse)
    def contest_home(request: Request, code: str):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        problems = contest_problems(c, contest, u) if started(contest, u) else []
        mine = my_problem_status(c, contest, u)
        news = db.all_(c, """SELECT * FROM clarifications WHERE contest_id=? AND public=1
                             AND answer IS NOT NULL ORDER BY answered_at DESC LIMIT 5""", (contest["id"],))
        return render(request, "contest.html", c, contest=contest, problems=problems, mine=mine,
                      news=news)

    def my_problem_status(c, contest, u) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for s in db.all_(c, """SELECT problem_id, status, verdict, score, max_score FROM submissions
                               WHERE contest_id=? AND user_id=? ORDER BY id""", (contest["id"], u["id"])):
            st = out.setdefault(s["problem_id"], {"tries": 0, "solved": False, "best": None,
                                                  "max": s["max_score"], "pending": False})
            st["tries"] += 1
            st["pending"] |= s["status"] != "done"
            st["solved"] |= s["verdict"] == "accepted"
            if s["score"] is not None and (st["best"] is None or s["score"] > st["best"]):
                st["best"], st["max"] = s["score"], s["max_score"]
        return out

    @app.get("/c/{code}/p/{label}", response_class=HTMLResponse)
    def problem_page(request: Request, code: str, label: str, lang: str | None = None):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        if not started(contest, u):
            raise Redirect(f"/c/{code}")
        problems = contest_problems(c, contest, u)
        prob = next((p for p in problems if p["label"] == label), None)
        if prob is None:
            raise HTTPException(404, "no such problem")
        statements = db.loads(prob["statements_json"], {})
        lang = lang if lang in statements else ("english" if "english" in statements
                                                 else next(iter(statements), None))
        return render(request, "problem.html", c, contest=contest, problems=problems, prob=prob,
                      st=statements.get(lang) if lang else None, lang=lang,
                      langs=list(statements), languages=[l for l in languages.LANGUAGES.values()
                                                         if l.available()],
                      last_lang=request.session.get("lang", "cpp"))

    @app.get("/problem-files/{slug}/{lang}/{name}")
    def problem_file(request: Request, slug: str, lang: str, name: str):
        c = conn()
        u = need_user(request, c)
        prob = db.one(c, "SELECT id FROM problems WHERE slug=?", (slug,))
        if prob is None:
            raise HTTPException(404)
        if not u["is_admin"]:
            ok = any(can_see(x, u) and started(x, u) for x in db.all_(c, """
                SELECT c.* FROM contests c JOIN contest_problems cp ON cp.contest_id=c.id
                WHERE cp.problem_id=?""", (prob["id"],)))
            if not ok:
                raise HTTPException(404)
        base = (settings.problems_dir / slug / "statements" / lang).resolve()
        path = (base / name).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/c/{code}/submit")
    async def submit(request: Request, code: str, label: str = Form(...), language: str = Form(...),
                     source: str = Form(""), file: UploadFile | None = File(None)):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        back = f"/c/{code}/p/{label}"
        if not started(contest, u):
            raise Redirect(f"/c/{code}")
        prob = next((p for p in contest_problems(c, contest, u) if p["label"] == label), None)
        if prob is None:
            raise HTTPException(404)
        if file is not None and file.filename:
            data = await file.read(MAX_SOURCE_BYTES + 1)
            source = data.decode("utf-8", "replace")
            language = languages.guess(file.filename) or language
        if language not in languages.LANGUAGES:
            flash(request, "Pick a language.")
            raise Redirect(back)
        if not source.strip():
            flash(request, "The solution is empty.")
            raise Redirect(back)
        if len(source.encode()) > MAX_SOURCE_BYTES:
            flash(request, f"The solution is over {MAX_SOURCE_BYTES // 1024} KB.")
            raise Redirect(back)
        last = db.one(c, "SELECT MAX(submitted_at) AS t FROM submissions WHERE user_id=?", (u["id"],))
        if not u["is_admin"] and last["t"] and time.time() - last["t"] < SUBMIT_COOLDOWN_SECONDS:
            flash(request, f"Wait {SUBMIT_COOLDOWN_SECONDS} seconds between submissions.")
            raise Redirect(back)
        c.execute("""INSERT INTO submissions (contest_id, problem_id, user_id, language, source,
                     submitted_at) VALUES (?,?,?,?,?,?)""",
                  (contest["id"], prob["id"], u["id"], language, source, time.time()))
        request.session["lang"] = language
        if time.time() > contest["end_at"]:
            flash(request, "The contest is over: this run is judged but doesn't count.")
        raise Redirect(f"/c/{code}/submissions")

    @app.get("/c/{code}/submissions", response_class=HTMLResponse)
    def my_submissions(request: Request, code: str):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        subs = db.all_(c, """
            SELECT s.id, s.submitted_at, s.language, s.status, s.verdict, s.score, s.max_score,
                   s.time_ms, s.memory_kb, s.failed_test, cp.label, p.name
            FROM submissions s JOIN problems p ON p.id=s.problem_id
            LEFT JOIN contest_problems cp ON cp.contest_id=s.contest_id AND cp.problem_id=s.problem_id
            WHERE s.contest_id=? AND s.user_id=? ORDER BY s.id DESC""", (contest["id"], u["id"]))
        return render(request, "submissions.html", c, contest=contest, subs=subs)

    @app.get("/c/{code}/s/{sid}", response_class=HTMLResponse)
    def submission_page(request: Request, code: str, sid: int):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        sub = db.one(c, """SELECT s.*, cp.label, p.name, p.slug FROM submissions s
                           JOIN problems p ON p.id=s.problem_id
                           LEFT JOIN contest_problems cp ON cp.contest_id=s.contest_id
                                AND cp.problem_id=s.problem_id
                           WHERE s.id=? AND s.contest_id=?""", (sid, contest["id"]))
        if sub is None or (sub["user_id"] != u["id"] and not u["is_admin"]):
            raise HTTPException(404)
        return render(request, "submission.html", c, contest=contest, sub=sub, admin_view=False)

    @app.get("/api/c/{code}/my-submissions")
    def my_submissions_json(request: Request, code: str):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        rows = db.all_(c, """SELECT id, status, verdict, score, max_score, time_ms, memory_kb,
                                    failed_test FROM submissions
                             WHERE contest_id=? AND user_id=? ORDER BY id DESC LIMIT 100""",
                       (contest["id"], u["id"]))
        return [dict(r) | {"verdict_label": VERDICT_LABELS.get(r["verdict"] or "", "")} for r in rows]

    @app.get("/c/{code}/standings", response_class=HTMLResponse)
    def standings_page(request: Request, code: str, division: str | None = None):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        if not u["is_admin"] and not contest["public_standings"]:
            return render(request, "standings.html", c, contest=contest, board=None, division=None)
        board = scoring.standings(c, contest, live=bool(u["is_admin"]), division=division or None)
        return render(request, "standings.html", c, contest=contest, board=board,
                      division=division, projector=request.query_params.get("projector"))

    @app.get("/c/{code}/clarifications", response_class=HTMLResponse)
    def clar_page(request: Request, code: str):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        items = db.all_(c, """
            SELECT cl.*, cp.label FROM clarifications cl
            LEFT JOIN contest_problems cp ON cp.contest_id=cl.contest_id AND cp.problem_id=cl.problem_id
            WHERE cl.contest_id=? AND (cl.user_id=? OR (cl.public=1 AND cl.answer IS NOT NULL))
            ORDER BY COALESCE(cl.answered_at, cl.created_at) DESC""", (contest["id"], u["id"]))
        problems = contest_problems(c, contest, u) if started(contest, u) else []
        return render(request, "clarifications.html", c, contest=contest, items=items,
                      problems=problems)

    @app.post("/c/{code}/clarifications")
    def ask(request: Request, code: str, question: str = Form(...), label: str = Form("")):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        prob = next((p for p in contest_problems(c, contest, u) if p["label"] == label), None)
        if question.strip():
            c.execute("""INSERT INTO clarifications (contest_id, user_id, problem_id, question,
                         created_at) VALUES (?,?,?,?,?)""",
                      (contest["id"], u["id"], prob["id"] if prob else None, question.strip()[:2000],
                       time.time()))
            flash(request, "Question sent.")
        raise Redirect(f"/c/{code}/clarifications")

    @app.get("/api/c/{code}/news")
    def news_json(request: Request, code: str):
        c = conn()
        u = need_user(request, c)
        contest = contest_by_code(c, code, u)
        row = db.one(c, """SELECT MAX(answered_at) AS t FROM clarifications WHERE contest_id=?
                           AND answer IS NOT NULL AND (public=1 OR user_id=?)""", (contest["id"], u["id"]))
        return {"latest": row["t"] or 0, "now": time.time()}

    # -- admin ---------------------------------------------------------------------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        c = conn()
        need_admin(request, c)
        urls, source = contestant_urls(c)
        qr = segno.make(urls[0]).svg_inline(scale=5, dark="#111", light="#fff", omitsize=True) if urls else ""
        queue = db.one(c, "SELECT COUNT(*) AS n FROM submissions WHERE status!='done'")["n"]
        pool = app.state.pool
        return render(request, "admin/home.html", c, urls=urls, qr=qr, queue=queue, url_source=source,
                      saved_url=db.get_setting(c, "public_url"),
                      workers=pool.count if pool else 0,
                      isolated=pool.isolated if pool else None,
                      jailed=pool.jailed if pool else None,
                      contests=db.all_(c, "SELECT * FROM contests ORDER BY start_at DESC"),
                      problems=db.one(c, "SELECT COUNT(*) AS n FROM problems")["n"],
                      users=db.one(c, "SELECT COUNT(*) AS n FROM users WHERE is_admin=0")["n"])

    def contestant_urls(c) -> tuple[list[str], str]:
        """The address(es) contestants should type, and where that came from.

        The IP comes first everywhere: ``.local`` names don't resolve in every
        browser (Android especially), so the mDNS name is the alternative.
        """
        saved = db.get_setting(c, "public_url")
        if saved:
            urls, source = [saved], "set"
        elif settings.public_url:
            urls, source = [settings.public_url], "env"
        elif config.in_container():
            urls, source = [], "container"  # our own addresses are the container's, useless to them
        else:
            urls, source = [f"http://{ip}:{settings.port}/" for ip in lan_addresses()], "detected"
        if app.state.mdns_host and urls:
            urls.insert(1, f"http://{app.state.mdns_host}:{settings.port}/")
        return urls, source

    @app.post("/admin/address")
    def admin_address(request: Request, url: str = Form("")):
        c = conn()
        need_admin(request, c)
        db.set_setting(c, "public_url", config.normalise_url(url))
        flash(request, "Address saved." if url.strip() else "Address cleared: using the detected one.")
        raise Redirect("/admin")

    # problems
    @app.get("/admin/problems", response_class=HTMLResponse)
    def admin_problems(request: Request):
        c = conn()
        need_admin(request, c)
        poll_imports()
        return render(request, "admin/problems.html", c,
                      problems=db.all_(c, "SELECT * FROM problems ORDER BY id DESC"),
                      imports=sorted(app.state.imports.values(), key=lambda j: -j["started"]))

    def poll_imports() -> None:
        for job in app.state.imports.values():
            if job["proc"] is not None and job["proc"].poll() is not None:
                job["status"] = "done" if job["proc"].returncode == 0 else "failed"
                job["proc"] = None
            job["log"] = Path(job["log_path"]).read_text(errors="replace") \
                if Path(job["log_path"]).exists() else ""

    @app.post("/admin/problems/import")
    async def admin_import(request: Request, package: UploadFile = File(...)):
        c = conn()
        need_admin(request, c)
        job_id = f"{int(time.time() * 1000)}"
        zip_path = settings.uploads_dir / f"{job_id}.zip"
        with open(zip_path, "wb") as f:
            while chunk := await package.read(1 << 20):
                f.write(chunk)
        log_path = settings.uploads_dir / f"{job_id}.log"
        # Imports compile and run code: a separate process keeps that off the
        # web server's threads (and a crash in a package off the server).
        proc = subprocess.Popen([sys.executable, "-m", "outpost", "import", str(zip_path),
                                 "--delete-zip"], stdout=open(log_path, "wb"),
                                stderr=subprocess.STDOUT, cwd=Path(__file__).parent.parent)
        app.state.imports[job_id] = {"id": job_id, "name": package.filename, "proc": proc,
                                     "status": "running", "log_path": str(log_path),
                                     "started": time.time(), "log": ""}
        flash(request, f"Importing {package.filename}…")
        raise Redirect("/admin/problems")

    @app.get("/admin/problems/{pid}", response_class=HTMLResponse)
    def admin_problem(request: Request, pid: int, lang: str | None = None):
        c = conn()
        need_admin(request, c)
        prob = db.one(c, "SELECT * FROM problems WHERE id=?", (pid,))
        if prob is None:
            raise HTTPException(404)
        statements = db.loads(prob["statements_json"], {})
        lang = lang if lang in statements else next(iter(statements), None)
        used = db.all_(c, """SELECT c.code, c.name, cp.label FROM contest_problems cp
                             JOIN contests c ON c.id=cp.contest_id WHERE cp.problem_id=?""", (pid,))
        return render(request, "admin/problem.html", c, prob=prob, st=statements.get(lang) if lang else None,
                      lang=lang, langs=list(statements), tests=db.loads(prob["tests_json"], []),
                      groups=db.loads(prob["groups_json"], []), used=used)

    @app.post("/admin/problems/{pid}/limits")
    def admin_problem_limits(request: Request, pid: int, time_limit_ms: int = Form(...),
                             memory_limit_mb: int = Form(...)):
        c = conn()
        need_admin(request, c)
        c.execute("UPDATE problems SET time_limit_ms=?, memory_limit_mb=? WHERE id=?",
                  (max(100, time_limit_ms), max(16, memory_limit_mb), pid))
        flash(request, "Limits saved. Rejudge if runs were already judged with the old ones.")
        raise Redirect(f"/admin/problems/{pid}")

    @app.post("/admin/problems/{pid}/delete")
    def admin_problem_delete(request: Request, pid: int):
        c = conn()
        need_admin(request, c)
        prob = db.one(c, "SELECT slug FROM problems WHERE id=?", (pid,))
        if prob:
            c.execute("DELETE FROM problems WHERE id=?", (pid,))
            subprocess.run(["rm", "-rf", str(settings.problems_dir / prob["slug"])], check=False)
            flash(request, f"Deleted {prob['slug']}.")
        raise Redirect("/admin/problems")

    # contests
    @app.get("/admin/contests", response_class=HTMLResponse)
    def admin_contests(request: Request):
        c = conn()
        need_admin(request, c)
        return render(request, "admin/contests.html", c,
                      contests=db.all_(c, "SELECT * FROM contests ORDER BY start_at DESC"),
                      divisions=all_divisions(c))

    def all_divisions(c) -> list[str]:
        return [r["division"] for r in db.all_(
            c, "SELECT DISTINCT division FROM users WHERE division!='' ORDER BY division")]

    def contest_form_values(form) -> dict:
        start = datetime.strptime(form["start"], "%Y-%m-%dT%H:%M").timestamp()
        minutes = int(form.get("duration") or 120)
        divisions = [d.strip() for d in (form.get("divisions") or "").split(",") if d.strip()]
        return {
            "name": form["name"].strip(), "start_at": start, "end_at": start + minutes * 60,
            "scoring": form.get("scoring", "icpc"),
            "penalty_minutes": int(form.get("penalty") or 20),
            "freeze_minutes": int(form.get("freeze") or 0),
            "divisions_json": db.dumps(divisions),
            "visible": int(bool(form.get("visible"))),
            "public_standings": int(bool(form.get("public_standings"))),
        }

    @app.post("/admin/contests")
    async def admin_contest_create(request: Request):
        c = conn()
        need_admin(request, c)
        form = await request.form()
        code = str(form["code"]).strip()
        if not code or db.one(c, "SELECT 1 FROM contests WHERE code=?", (code,)):
            flash(request, "Contest code is empty or taken.")
            raise Redirect("/admin/contests")
        vals = contest_form_values(form)
        cols = ["code", *vals, "created_at"]
        c.execute(f"INSERT INTO contests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  [code, *vals.values(), time.time()])
        raise Redirect(f"/admin/contests/{code}")

    @app.get("/admin/contests/{code}", response_class=HTMLResponse)
    def admin_contest(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        return render(request, "admin/contest.html", c, contest=contest, admin_page=True,
                      problems=contest_problems(c, contest, u),
                      all_problems=db.all_(c, "SELECT id, slug, name, status FROM problems ORDER BY slug"),
                      divisions=all_divisions(c),
                      frozen=scoring.is_frozen(contest, time.time()),
                      pending=db.one(c, """SELECT COUNT(*) AS n FROM clarifications
                                           WHERE contest_id=? AND answer IS NULL""",
                                     (contest["id"],))["n"])

    @app.post("/admin/contests/{code}")
    async def admin_contest_update(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        vals = contest_form_values(await request.form())
        c.execute(f"UPDATE contests SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?",
                  [*vals.values(), contest["id"]])
        flash(request, "Saved.")
        raise Redirect(f"/admin/contests/{code}")

    @app.post("/admin/contests/{code}/problems")
    def admin_contest_add_problem(request: Request, code: str, problem_id: int = Form(...),
                                  label: str = Form(""), divisions: str = Form("")):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        existing = contest_problems(c, contest, u)
        label = label.strip() or chr(ord("A") + len(existing))
        divs = [d.strip() for d in divisions.split(",") if d.strip()]
        c.execute("""INSERT OR REPLACE INTO contest_problems (contest_id, problem_id, label, position,
                     divisions_json) VALUES (?,?,?,?,?)""",
                  (contest["id"], problem_id, label, len(existing), db.dumps(divs)))
        raise Redirect(f"/admin/contests/{code}")

    @app.post("/admin/contests/{code}/problems/order")
    async def admin_contest_order(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        form = await request.form()
        for p in contest_problems(c, contest, u):
            if form.get(f"remove_{p['id']}"):
                c.execute("DELETE FROM contest_problems WHERE contest_id=? AND problem_id=?",
                          (contest["id"], p["id"]))
                continue
            label = str(form.get(f"label_{p['id']}", p["label"])).strip() or p["label"]
            pos = int(form.get(f"pos_{p['id']}") or p["position"])
            divs = [d.strip() for d in str(form.get(f"div_{p['id']}", "")).split(",") if d.strip()]
            c.execute("""UPDATE contest_problems SET label=?, position=?, divisions_json=?
                         WHERE contest_id=? AND problem_id=?""",
                      (label, pos, db.dumps(divs), contest["id"], p["id"]))
        flash(request, "Problems saved.")
        raise Redirect(f"/admin/contests/{code}")

    @app.post("/admin/contests/{code}/unfreeze")
    def admin_unfreeze(request: Request, code: str, value: int = Form(1)):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        c.execute("UPDATE contests SET unfrozen=? WHERE id=?", (value, contest["id"]))
        flash(request, "Standings unfrozen." if value else "Freeze restored.")
        raise Redirect(f"/admin/contests/{code}")

    @app.get("/admin/contests/{code}/standings.csv")
    def admin_standings_csv(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        board = scoring.standings(c, contest, live=True)
        return Response(scoring.to_csv(board), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{code}-standings.csv"'})

    @app.get("/admin/contests/{code}/export.json")
    def admin_export(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        subs = db.all_(c, """SELECT s.id, u.username, u.division, cp.label, p.slug, s.language,
                                    s.submitted_at, s.verdict, s.score, s.max_score, s.time_ms,
                                    s.memory_kb, s.failed_test, s.source
                             FROM submissions s JOIN users u ON u.id=s.user_id
                             JOIN problems p ON p.id=s.problem_id
                             LEFT JOIN contest_problems cp ON cp.contest_id=s.contest_id
                                  AND cp.problem_id=s.problem_id
                             WHERE s.contest_id=? ORDER BY s.id""", (contest["id"],))
        body = {"contest": dict(contest), "submissions": [dict(s) for s in subs]}
        return JSONResponse(body, headers={
            "Content-Disposition": f'attachment; filename="{code}-export.json"'})

    # submissions
    @app.get("/admin/submissions", response_class=HTMLResponse)
    def admin_submissions(request: Request, contest: str = "", problem: str = "", verdict: str = "",
                          user: str = "", page: int = 1):
        c = conn()
        need_admin(request, c)
        where, params = submission_filter(c, contest, problem, verdict, user)
        subs = db.all_(c, f"""
            SELECT s.id, s.submitted_at, s.language, s.status, s.verdict, s.score, s.max_score,
                   s.time_ms, s.memory_kb, s.failed_test, u.username, p.slug, c.code AS contest_code
            FROM submissions s JOIN users u ON u.id=s.user_id JOIN problems p ON p.id=s.problem_id
            LEFT JOIN contests c ON c.id=s.contest_id
            WHERE {where} ORDER BY s.id DESC LIMIT 100 OFFSET ?""", [*params, (page - 1) * 100])
        total = db.one(c, f"""SELECT COUNT(*) AS n FROM submissions s JOIN users u ON u.id=s.user_id
                              JOIN problems p ON p.id=s.problem_id LEFT JOIN contests c ON c.id=s.contest_id
                              WHERE {where}""", params)["n"]
        return render(request, "admin/submissions.html", c, subs=subs, total=total, page=page,
                      f={"contest": contest, "problem": problem, "verdict": verdict, "user": user},
                      verdicts=VERDICTS,
                      contests=db.all_(c, "SELECT code FROM contests ORDER BY start_at DESC"))

    def submission_filter(c, contest, problem, verdict, user) -> tuple[str, list]:
        where, params = ["1=1"], []
        if contest:
            where.append("c.code=?")
            params.append(contest)
        if problem:
            where.append("p.slug=?")
            params.append(problem)
        if verdict:
            where.append("s.verdict=?" if verdict != "pending" else "s.status!='done'")
            if verdict != "pending":
                params.append(verdict)
        if user:
            where.append("u.username=?")
            params.append(user)
        return " AND ".join(where), params

    @app.get("/admin/submissions/{sid}", response_class=HTMLResponse)
    def admin_submission(request: Request, sid: int):
        c = conn()
        need_admin(request, c)
        sub = db.one(c, """SELECT s.*, u.username, p.slug, p.name, cp.label, c.code AS contest_code
                           FROM submissions s JOIN users u ON u.id=s.user_id
                           JOIN problems p ON p.id=s.problem_id
                           LEFT JOIN contests c ON c.id=s.contest_id
                           LEFT JOIN contest_problems cp ON cp.contest_id=s.contest_id
                                AND cp.problem_id=s.problem_id
                           WHERE s.id=?""", (sid,))
        if sub is None:
            raise HTTPException(404)
        return render(request, "submission.html", c, contest=None, sub=sub, admin_view=True)

    @app.post("/admin/rejudge")
    async def admin_rejudge(request: Request):
        c = conn()
        need_admin(request, c)
        form = await request.form()
        if form.get("id"):
            ids = [int(str(form["id"]))]
        else:
            where, params = submission_filter(c, str(form.get("contest", "")), str(form.get("problem", "")),
                                              str(form.get("verdict", "")), str(form.get("user", "")))
            ids = [r["id"] for r in db.all_(c, f"""
                SELECT s.id FROM submissions s JOIN users u ON u.id=s.user_id
                JOIN problems p ON p.id=s.problem_id LEFT JOIN contests c ON c.id=s.contest_id
                WHERE {where}""", params)]
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            c.execute(f"""UPDATE submissions SET status='pending', verdict=NULL, score=NULL
                          WHERE id IN ({','.join('?' * len(part))})""", part)
        flash(request, f"Queued {len(ids)} submission(s) for rejudging.")
        raise Redirect(str(form.get("back") or "/admin/submissions"))

    # users
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request):
        c = conn()
        need_admin(request, c)
        batch = request.session.get("batch")
        return render(request, "admin/users.html", c, batch=batch,
                      created=app.state.batches.get(batch or "", []),
                      users=db.all_(c, "SELECT * FROM users ORDER BY is_admin DESC, division, username"))

    @app.post("/admin/users/import")
    def admin_users_import(request: Request, rows: str = Form(...), admin: str = Form("")):
        """Lines of ``username, name, division[, password]``; missing passwords are generated."""
        c = conn()
        need_admin(request, c)
        created, skipped = [], []
        for rec in csv.reader(io.StringIO(rows)):
            rec = [x.strip() for x in rec]
            if not rec or not rec[0] or rec[0].lower() == "username":
                continue
            username, name = rec[0], rec[1] if len(rec) > 1 else ""
            division = rec[2] if len(rec) > 2 else ""
            password = rec[3] if len(rec) > 3 and rec[3] else auth.generate_password()
            if db.one(c, "SELECT 1 FROM users WHERE username=?", (username,)):
                skipped.append(username)
                continue
            auth.create_user(c, username, password, display_name=name, division=division,
                             is_admin=bool(admin))
            created.append({"username": username, "name": name, "division": division,
                            "password": password})
        remember_batch(request, created)
        flash(request, f"Created {len(created)} account(s)"
                       + (f"; skipped existing: {', '.join(skipped)}" if skipped else "") + ".")
        raise Redirect("/admin/users")

    @app.post("/admin/users/{uid}")
    def admin_user_action(request: Request, uid: int, action: str = Form(...),
                          division: str = Form(""), display_name: str = Form("")):
        c = conn()
        me = need_admin(request, c)
        target = db.one(c, "SELECT * FROM users WHERE id=?", (uid,))
        if target is None:
            raise HTTPException(404)
        if action == "reset":
            pw = auth.generate_password()
            c.execute("UPDATE users SET password_hash=?, session_token=NULL WHERE id=?",
                      (auth.hash_password(pw), uid))
            remember_batch(request, [{"username": target["username"], "name": target["display_name"],
                                      "division": target["division"], "password": pw}])
            flash(request, f"New password for {target['username']}.")
        elif action == "kick":
            c.execute("UPDATE users SET session_token=NULL WHERE id=?", (uid,))
            flash(request, f"Signed {target['username']} out.")
        elif action == "edit":
            c.execute("UPDATE users SET division=?, display_name=? WHERE id=?",
                      (division.strip(), display_name.strip(), uid))
            flash(request, "Saved.")
        elif action == "delete" and uid != me["id"]:
            c.execute("DELETE FROM users WHERE id=?", (uid,))
            flash(request, f"Deleted {target['username']}.")
        raise Redirect("/admin/users")

    def remember_batch(request: Request, people: list[dict]) -> None:
        token = auth.generate_password(12)
        app.state.batches[token] = people
        request.session["batch"] = token

    @app.post("/admin/forget-passwords")
    def admin_forget(request: Request):
        c = conn()
        need_admin(request, c)
        app.state.batches.pop(request.session.pop("batch", "") or "", None)
        raise Redirect("/admin/users")

    @app.get("/admin/slips/{batch}", response_class=HTMLResponse)
    def admin_slips(request: Request, batch: str):
        c = conn()
        need_admin(request, c)
        urls, _ = contestant_urls(c)
        return render(request, "admin/slips.html", c, people=app.state.batches.get(batch, []),
                      url=urls[0] if urls else "",
                      alt_url=urls[1] if len(urls) > 1 and ".local" in urls[1] else "")

    # clarifications
    @app.get("/admin/contests/{code}/clarifications", response_class=HTMLResponse)
    def admin_clars(request: Request, code: str):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        items = db.all_(c, """
            SELECT cl.*, u.username, cp.label FROM clarifications cl
            LEFT JOIN users u ON u.id=cl.user_id
            LEFT JOIN contest_problems cp ON cp.contest_id=cl.contest_id AND cp.problem_id=cl.problem_id
            WHERE cl.contest_id=? ORDER BY (cl.answer IS NULL) DESC, cl.created_at DESC""",
                       (contest["id"],))
        return render(request, "admin/clarifications.html", c, contest=contest, items=items,
                      admin_page=True)

    @app.post("/admin/contests/{code}/clarifications")
    def admin_clar_answer(request: Request, code: str, answer: str = Form(...), cid: int = Form(0),
                          public: str = Form("")):
        c = conn()
        u = need_admin(request, c)
        contest = contest_by_code(c, code, u)
        if cid:
            c.execute("UPDATE clarifications SET answer=?, public=?, answered_at=? WHERE id=? AND contest_id=?",
                      (answer.strip(), int(bool(public)), time.time(), cid, contest["id"]))
        elif answer.strip():  # an announcement
            c.execute("""INSERT INTO clarifications (contest_id, user_id, question, answer, public,
                         created_at, answered_at) VALUES (?, NULL, '', ?, 1, ?, ?)""",
                      (contest["id"], answer.strip(), time.time(), time.time()))
        raise Redirect(f"/admin/contests/{code}/clarifications")

    @app.get("/healthz", response_class=PlainTextResponse)
    def health():
        return "ok"

    return app


def lan_addresses() -> list[str]:
    """IPv4 addresses contestants can reach us on (loopback last, as a fallback)."""
    found: list[str] = []
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True,
                             timeout=2).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                ip = parts[parts.index("inet") + 1].split("/")[0]
                if not ip.startswith("127.") and not ip.startswith("172.17."):
                    found.append(ip)
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in found:
                    found.append(ip)
        except OSError:
            pass
    # Private ranges first: that's the hotspot.
    found.sort(key=lambda ip: not ip.startswith(("192.168.", "10.", "172.")))
    return found + ["127.0.0.1"]
