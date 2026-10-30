"""Judge workers: separate processes that take submissions off the queue.

The queue is the ``submissions`` table itself (``status='pending'``). A worker
claims one row atomically, judges it, writes the verdict. On start-up anything
left ``judging`` by a crash goes back to ``pending``, so a power cut costs a
re-judge, not a lost submission.

Workers are processes, not threads: each is single-threaded, which is what
forking a sandboxed child safely needs, and each gets its own sandbox uid.
"""

from __future__ import annotations

import fcntl
import multiprocessing as mp
import os
import signal
import sqlite3
import time
import traceback

from . import db, languages
from .config import Settings
from .judge import JUDGE_ERROR, Judge, ProblemSpec
from .sandbox import Sandbox, build_launcher, jail_available, network_isolation_available

POLL_SECONDS = 0.25


def spec_for(row: sqlite3.Row, settings: Settings) -> ProblemSpec:
    return ProblemSpec(
        dir=settings.problems_dir / row["slug"],
        time_limit_ms=row["time_limit_ms"],
        memory_limit_mb=row["memory_limit_mb"],
        tests=db.loads(row["tests_json"], []),
        groups=db.loads(row["groups_json"], []),
        interactive=bool(row["interactive"]),
        checker=row["checker"],
    )


# A claimed submission must be judged within this long, or it goes back to the
# queue for someone else: covers a remote judge laptop that died or left.
LEASE_SECONDS = 600


def requeue_stale(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE submissions SET status='pending', judged_by=NULL, lease_until=NULL "
                        "WHERE status='judging'").rowcount


def claim(conn: sqlite3.Connection, judge_name: str = "local") -> int | None:
    now = db.now()
    conn.execute("UPDATE submissions SET status='pending', judged_by=NULL, lease_until=NULL "
                 "WHERE status='judging' AND lease_until < ?", (now,))
    row = db.one(conn, """
        UPDATE submissions SET status='judging', judged_by=?, lease_until=?
        WHERE id = (SELECT id FROM submissions WHERE status='pending' ORDER BY id LIMIT 1)
          AND status='pending'
        RETURNING id""", (judge_name, now + LEASE_SECONDS))
    return row["id"] if row else None


def job_for(conn: sqlite3.Connection, sub_id: int) -> tuple[sqlite3.Row, sqlite3.Row | None, str]:
    """(submission, problem, mode) for a claimed submission."""
    sub = db.one(conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))
    prob = db.one(conn, "SELECT * FROM problems WHERE id=?", (sub["problem_id"],))
    mode = "ioi"
    if sub["contest_id"]:
        contest = db.one(conn, "SELECT scoring FROM contests WHERE id=?", (sub["contest_id"],))
        if contest and contest["scoring"] == "icpc":
            mode = "icpc"
    return sub, prob, mode


def outcome_dict(out) -> dict:
    return {"verdict": out.verdict, "score": out.score, "max_score": out.max_score, "time_ms": out.time_ms,
            "memory_kb": out.memory_kb, "failed_test": out.failed_test, "compile_output": out.compile_output,
            "tests": out.test_dicts(), "groups": out.groups}


def error_outcome() -> dict:
    return {"verdict": JUDGE_ERROR, "score": 0, "compile_output": traceback.format_exc()[-4000:]}


def save_outcome(conn: sqlite3.Connection, sub_id: int, judge_name: str, out: dict) -> bool:
    """Write a verdict, only if ``judge_name`` still holds the submission (its lease
    may have run out, or an admin may have queued a rejudge meanwhile)."""
    return conn.execute("""
        UPDATE submissions SET status='done', verdict=?, score=?, max_score=?, time_ms=?, memory_kb=?,
            failed_test=?, compile_output=?, test_results_json=?, group_results_json=?, judged_at=?,
            lease_until=NULL
        WHERE id=? AND status='judging' AND judged_by=?""",
        (out["verdict"], out.get("score"), out.get("max_score"), out.get("time_ms"), out.get("memory_kb"),
         out.get("failed_test"), out.get("compile_output"), db.dumps(out.get("tests") or []),
         db.dumps(out.get("groups") or []), db.now(), sub_id, judge_name)).rowcount == 1


def judge_submission(conn: sqlite3.Connection, judge: Judge, settings: Settings, sub_id: int,
                     judge_name: str = "local") -> None:
    sub, prob, mode = job_for(conn, sub_id)
    try:
        if prob is None or prob["status"] != "ready":
            raise RuntimeError("problem is not ready")
        out = outcome_dict(judge.judge(spec_for(prob, settings), sub["language"], sub["source"], mode))
    except Exception:
        out = error_outcome()
    save_outcome(conn, sub_id, judge_name, out)


# Remote judges get their own boxes and uids, so one started on the server's
# own laptop can't collide with the local workers.
REMOTE_UID_OFFSET = 500


def make_judge(settings: Settings, n: int, isolate: bool, jail: bool = False, kind: str = "worker") -> Judge:
    work = settings.boxes_dir / f"{kind}-{n}"
    uid = settings.sandbox_uid_base + n + (REMOTE_UID_OFFSET if kind == "remote" else 0)
    sandbox = Sandbox(work / "box", uid=uid, isolate_network=isolate,
                      jail=jail, readonly=[settings.pch_dir, settings.runtime_dir],
                      launcher=build_launcher(settings.runtime_dir))
    return Judge(sandbox, work / "io", pch_dir=settings.pch_dir)


def protections(settings: Settings) -> tuple[bool, bool]:
    """(network isolation, filesystem jail) — what this machine allows, probed once."""
    return network_isolation_available(), jail_available(settings.boxes_dir)


def _die_with_parent(parent: int) -> None:
    """Make sure a worker never outlives the server that started it.

    An orphaned worker keeps judging into the same boxes as the next server's
    workers, and two judges in one box wreck each other's runs. Linux can
    signal us the moment the parent dies; the pid check in the loop covers the
    rest.
    """
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass
    if os.getppid() != parent:  # it already died while we were starting
        raise SystemExit(0)


def lock_box(settings: Settings, n: int, kind: str = "worker"):
    """Hold worker-N's box for as long as this process lives (released on exit)."""
    work = settings.boxes_dir / f"{kind}-{n}"
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o711)
    handle = open(work / "lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"{kind}-{n}'s box is in use by another judge process") from None
    return handle


def run_worker(settings: Settings, n: int, isolate: bool, jail: bool, stop=None,
               parent: int | None = None) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent decides when we stop
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    if parent is not None:
        _die_with_parent(parent)
    box_lock = lock_box(settings, n)  # noqa: F841 - held until exit
    conn = db.connect(settings.db_path)
    judge = make_judge(settings, n, isolate, jail)
    while stop is None or not stop.is_set():
        if parent is not None and os.getppid() != parent:
            break
        sub_id = claim(conn, f"local-{n}")
        if sub_id is None:
            time.sleep(POLL_SECONDS)
            continue
        judge_submission(conn, judge, settings, sub_id, f"local-{n}")


def drain(settings: Settings, n: int = 0) -> int:
    """Judge everything pending in this process, then return (tests, CLI)."""
    languages.prepare_pch(settings.pch_dir, log=lambda _: None)
    box_lock = lock_box(settings, n)  # noqa: F841 - held until we return
    conn = db.connect(settings.db_path)
    judge = make_judge(settings, n, *protections(settings))
    count = 0
    while (sub_id := claim(conn, f"local-{n}")) is not None:
        judge_submission(conn, judge, settings, sub_id, f"local-{n}")
        count += 1
    conn.close()
    return count


class WorkerPool:
    def __init__(self, settings: Settings, count: int):
        self.settings, self.count = settings, count
        ctx = mp.get_context("spawn")
        self.stop = ctx.Event()
        settings.ensure_dirs()
        self.isolated, self.jailed = protections(settings)
        self.procs = [ctx.Process(target=run_worker,
                                  args=(settings, n, self.isolated, self.jailed, self.stop, os.getpid()),
                                  name=f"outpost-judge-{n}", daemon=True)
                      for n in range(count)]

    def start(self) -> None:
        languages.prepare_pch(self.settings.pch_dir)
        conn = db.connect(self.settings.db_path)
        requeue_stale(conn)
        conn.close()
        for p in self.procs:
            p.start()

    def shutdown(self, timeout: float = 5) -> None:
        self.stop.set()
        for p in self.procs:
            p.join(timeout)
            if p.is_alive():
                p.terminate()
