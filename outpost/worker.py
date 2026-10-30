"""Judge workers: separate processes that take submissions off the queue.

The queue is the ``submissions`` table itself (``status='pending'``). A worker
claims one row atomically, judges it, writes the verdict. On start-up anything
left ``judging`` by a crash goes back to ``pending``, so a power cut costs a
re-judge, not a lost submission.

Workers are processes, not threads: each is single-threaded, which is what
forking a sandboxed child safely needs, and each gets its own sandbox uid.
"""

from __future__ import annotations

import multiprocessing as mp
import signal
import sqlite3
import time
import traceback

from . import db
from .config import Settings
from .judge import JUDGE_ERROR, Judge, ProblemSpec
from .sandbox import Sandbox, network_isolation_available

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


def requeue_stale(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE submissions SET status='pending' WHERE status='judging'").rowcount


def claim(conn: sqlite3.Connection) -> int | None:
    row = db.one(conn, """
        UPDATE submissions SET status='judging'
        WHERE id = (SELECT id FROM submissions WHERE status='pending' ORDER BY id LIMIT 1)
          AND status='pending'
        RETURNING id""")
    return row["id"] if row else None


def judge_submission(conn: sqlite3.Connection, judge: Judge, settings: Settings, sub_id: int) -> None:
    sub = db.one(conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))
    prob = db.one(conn, "SELECT * FROM problems WHERE id=?", (sub["problem_id"],))
    mode = "ioi"
    if sub["contest_id"]:
        contest = db.one(conn, "SELECT scoring FROM contests WHERE id=?", (sub["contest_id"],))
        if contest and contest["scoring"] == "icpc":
            mode = "icpc"
    try:
        if prob is None or prob["status"] != "ready":
            raise RuntimeError("problem is not ready")
        out = judge.judge(spec_for(prob, settings), sub["language"], sub["source"], mode)
        conn.execute("""
            UPDATE submissions SET status='done', verdict=?, score=?, max_score=?, time_ms=?,
                memory_kb=?, failed_test=?, compile_output=?, test_results_json=?,
                group_results_json=?, judged_at=?
            WHERE id=?""",
            (out.verdict, out.score, out.max_score, out.time_ms, out.memory_kb, out.failed_test,
             out.compile_output, db.dumps(out.test_dicts()), db.dumps(out.groups), db.now(), sub_id))
    except Exception:
        conn.execute("""UPDATE submissions SET status='done', verdict=?, score=0, compile_output=?,
                        judged_at=? WHERE id=?""",
                     (JUDGE_ERROR, traceback.format_exc()[-4000:], db.now(), sub_id))


def make_judge(settings: Settings, n: int, isolate: bool) -> Judge:
    work = settings.boxes_dir / f"worker-{n}"
    return Judge(Sandbox(work / "box", uid=settings.sandbox_uid_base + n, isolate_network=isolate),
                 work / "io")


def run_worker(settings: Settings, n: int, isolate: bool, stop=None) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent decides when we stop
    conn = db.connect(settings.db_path)
    judge = make_judge(settings, n, isolate)
    while stop is None or not stop.is_set():
        sub_id = claim(conn)
        if sub_id is None:
            time.sleep(POLL_SECONDS)
            continue
        judge_submission(conn, judge, settings, sub_id)


def drain(settings: Settings, n: int = 0) -> int:
    """Judge everything pending in this process, then return (tests, CLI)."""
    conn = db.connect(settings.db_path)
    judge = make_judge(settings, n, network_isolation_available())
    count = 0
    while (sub_id := claim(conn)) is not None:
        judge_submission(conn, judge, settings, sub_id)
        count += 1
    conn.close()
    return count


class WorkerPool:
    def __init__(self, settings: Settings, count: int):
        self.settings, self.count = settings, count
        ctx = mp.get_context("spawn")
        self.stop = ctx.Event()
        isolate = network_isolation_available()
        self.isolated = isolate
        self.procs = [ctx.Process(target=run_worker, args=(settings, n, isolate, self.stop),
                                  name=f"outpost-judge-{n}", daemon=True)
                      for n in range(count)]

    def start(self) -> None:
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
