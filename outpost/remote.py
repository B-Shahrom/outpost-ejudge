"""Remote judges: another laptop on the network adds judging capacity.

    outpost worker --server http://192.168.137.1:8000/ --token <token from the dashboard>

The worker claims submissions over HTTP, fetches each problem's tests once
(cached by version under its own data directory), judges with the same
sandbox as the server, and posts the verdict back. The server holds the only
database; a worker needs root (for the sandbox), g++ and the JDK/Python the
contest uses, and nothing else.

A worker that dies mid-judge costs nothing but time: the server gives every
claim a lease, and an expired lease puts the submission back in the queue.
"""

from __future__ import annotations

import fcntl
import io
import json
import multiprocessing as mp
import os
import shutil
import signal
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import languages, polygon
from .config import Settings
from .judge import JUDGE_ERROR, ProblemSpec
from .worker import _die_with_parent, lock_box, make_judge, outcome_dict, protections

POLL_SECONDS = 1.0


class Server:
    def __init__(self, url: str, token: str, name: str):
        self.url = url.rstrip("/")
        self.token = token
        self.name = name

    def _call(self, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, bytes]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def claim(self) -> dict | None:
        code, body = self._call("/api/worker/claim", {"name": self.name})
        if code == 204:
            return None
        if code == 401:
            raise SystemExit("The server refused the token. Copy the command from the admin dashboard again.")
        if code != 200:
            raise OSError(f"claim: HTTP {code}")
        return json.loads(body)

    def problem_zip(self, slug: str) -> bytes:
        code, body = self._call(f"/api/worker/problem/{slug}", timeout=600)
        if code != 200:
            raise OSError(f"problem {slug}: HTTP {code}")
        return body

    def report(self, sub_id: int, outcome: dict) -> bool:
        code, body = self._call(f"/api/worker/result/{sub_id}", {"name": self.name, "outcome": outcome})
        if code != 200:
            raise OSError(f"result: HTTP {code}")
        return bool(json.loads(body).get("saved"))


def cached_problem(server: Server, settings: Settings, job: dict, log) -> Path:
    """The problem's directory in the local cache, downloaded if its version changed."""
    slug, version = job["problem"]["slug"], job["problem"]["version"]
    cache = settings.data_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    pdir = cache / slug
    stamp = cache / f"{slug}.version"
    with open(cache / f"{slug}.lock", "w") as lock:  # several worker processes share the cache
        fcntl.flock(lock, fcntl.LOCK_EX)
        if pdir.exists() and stamp.exists() and stamp.read_text() == version:
            return pdir
        log(f"fetching {slug} (version {version})")
        data = server.problem_zip(slug)
        tmp = cache / f".{slug}.{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        zip_path = cache / f".{slug}.{os.getpid()}.zip"
        zip_path.write_bytes(data)
        try:
            polygon.safe_extract(zip_path, tmp)
        finally:
            zip_path.unlink()
        polygon.rebuild_binaries(tmp, log)  # binaries built here, for this machine
        for name in ("check", "interactor"):
            if (tmp / name).exists():
                (tmp / name).chmod(0o700)
        shutil.rmtree(pdir, ignore_errors=True)
        os.replace(tmp, pdir)
        stamp.write_text(version)
        return pdir


def run(server_url: str, token: str, settings: Settings, n: int, isolate: bool, jail: bool,
        stop=None, parent: int | None = None) -> None:
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent decides when we stop
    if parent is not None:
        _die_with_parent(parent)  # never outlive `outpost worker` (it may be killed)
    name = f"{socket.gethostname()}/{n}"
    server = Server(server_url, token, name)
    box_lock = lock_box(settings, n, kind="remote")  # noqa: F841 - held until exit
    judge = make_judge(settings, n, isolate, jail, kind="remote")
    log = lambda s: print(f"[{name}] {s}", flush=True)  # noqa: E731
    backoff = 1.0
    while stop is None or not stop.is_set():
        if parent is not None and os.getppid() != parent:
            break
        try:
            job = server.claim()
            backoff = 1.0
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            try:
                p = job["problem"]
                spec = ProblemSpec(dir=cached_problem(server, settings, job, log),
                                   time_limit_ms=p["time_limit_ms"], memory_limit_mb=p["memory_limit_mb"],
                                   tests=p["tests"], groups=p["groups"], interactive=p["interactive"],
                                   checker=p["checker"])
                out = outcome_dict(judge.judge(spec, job["language"], job["source"], job["mode"]))
            except Exception:
                out = {"verdict": JUDGE_ERROR, "score": 0,
                       "compile_output": f"remote judge {name}:\n" + traceback.format_exc()[-3800:]}
            saved = server.report(job["id"], out)
            log(f"#{job['id']} {out['verdict']}" + ("" if saved else " (not saved: re-queued meanwhile)"))
        except (OSError, ValueError) as e:  # server restarting, Wi-Fi hiccup: keep trying
            log(f"server unreachable ({e}); retrying in {backoff:.0f} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def serve_forever(server_url: str, token: str, settings: Settings, count: int) -> None:
    settings.ensure_dirs()
    languages.prepare_pch(settings.pch_dir)
    isolate, jail = protections(settings)
    print(f"Outpost remote judge: {count} worker(s) for {server_url}, network isolation "
          f"{'on' if isolate else 'OFF'}, filesystem jail {'on' if jail else 'OFF'}", flush=True)
    if count == 1:
        run(server_url, token, settings, 0, isolate, jail)
        return
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=run, args=(server_url, token, settings, n, isolate, jail, None, os.getpid()),
                         daemon=True) for n in range(count)]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


def problem_archive(problem_dir: Path) -> bytes:
    """What a remote judge needs of a problem: tests, checker/interactor (sources
    when we have them, binaries otherwise). Statements stay on the server."""
    buf = io.BytesIO()
    has_src = (problem_dir / "src").is_dir()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(problem_dir.rglob("*")):
            rel = f.relative_to(problem_dir)
            if not f.is_file() or rel.parts[0] == "statements":
                continue
            if has_src and len(rel.parts) == 1 and f.name in ("check", "interactor"):
                continue
            zf.write(f, str(rel))
    return buf.getvalue()
