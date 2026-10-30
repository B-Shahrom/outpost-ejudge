"""Remote judges: a real HTTP server with no local workers, judged from afar."""
from __future__ import annotations

import dataclasses
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from outpost import db, remote
from outpost.app import create_app
from outpost.worker import LEASE_SECONDS

AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
WA = "#include <bits/stdc++.h>\nint main(){std::cout<<0;}"


@pytest.fixture(scope="module")
def server(settings, problems):
    app = create_app(settings)  # no WorkerPool: nothing judges locally
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    c = db.connect(settings.db_path)
    token = db.get_setting(c, "worker_token") or (db.set_setting(c, "worker_token", "tok-123") or "tok-123")
    yield app, f"http://127.0.0.1:{port}", token
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def worker_settings(settings):
    data = Path(tempfile.mkdtemp(prefix="outpost-remote-"))
    data.chmod(0o711)
    s = dataclasses.replace(settings, data_dir=data)
    s.ensure_dirs()
    return s


def submit(settings, problem_id, source):
    c = db.connect(settings.db_path)
    uid = (db.one(c, "SELECT id FROM users WHERE username='remote-user'") or
           {"id": c.execute("INSERT INTO users (username, password_hash, created_at) "
                            "VALUES ('remote-user','x',0)").lastrowid})["id"]
    return c.execute("INSERT INTO submissions (problem_id, user_id, language, source, submitted_at) "
                     "VALUES (?,?,?,?,?)", (problem_id, uid, "cpp", source, time.time())).lastrowid


def wait_done(settings, ids, timeout=120):
    c = db.connect(settings.db_path)
    end = time.time() + timeout
    while time.time() < end:
        rows = [db.one(c, "SELECT status, verdict, judged_by FROM submissions WHERE id=?", (i,)) for i in ids]
        if all(r["status"] == "done" for r in rows):
            return rows
        time.sleep(0.3)
    raise AssertionError(f"not judged in time: {[dict(r) for r in rows]}")


def test_remote_judge_end_to_end(server, settings, problems, worker_settings):
    app, url, token = server
    ids = [submit(settings, problems["a-plus-b"], AB), submit(settings, problems["a-plus-b"], WA),
           submit(settings, problems["guess"], open(Path(__file__).parent / "packages.py").read()
                  .split('GUESS_MAIN = r"""')[1].split('"""')[0])]
    stop = threading.Event()
    t = threading.Thread(target=remote.run, args=(url, token, worker_settings, 0, False, False, stop), daemon=True)
    t.start()
    try:
        rows = wait_done(settings, ids)
    finally:
        stop.set()
        t.join(10)
    assert [r["verdict"] for r in rows] == ["accepted", "wrong_answer", "accepted"]
    assert all(r["judged_by"].startswith("remote:") for r in rows)
    cache = worker_settings.data_dir / "cache"
    assert (cache / "a-plus-b" / "tests" / "01.a").exists() and (cache / "a-plus-b" / "check").exists()
    assert not (cache / "a-plus-b" / "statements").exists()      # statements stay on the server
    assert sum(s["judged"] for s in app.state.remote_seen.values()) >= 3


def test_bad_token_is_refused(server):
    _, url, _ = server
    with pytest.raises(SystemExit, match="refused the token"):
        remote.Server(url, "wrong", "x").claim()


def test_lease_expiry_requeues_and_late_results_are_dropped(server, settings, problems):
    _, url, token = server
    sid = submit(settings, problems["a-plus-b"], AB)
    dead = remote.Server(url, token, "dead-laptop")
    job = dead.claim()
    assert job["id"] == sid
    c = db.connect(settings.db_path)
    # The laptop vanishes; its lease runs out.
    c.execute("UPDATE submissions SET lease_until=? WHERE id=?", (time.time() - 1, sid))
    alive = remote.Server(url, token, "alive-laptop")
    again = alive.claim()
    assert again["id"] == sid
    assert dead.report(sid, {"verdict": "wrong_answer", "score": 0}) is False   # too late
    assert alive.report(sid, {"verdict": "accepted", "score": 100, "max_score": 100}) is True
    row = db.one(c, "SELECT verdict, judged_by FROM submissions WHERE id=?", (sid,))
    assert row["verdict"] == "accepted" and row["judged_by"] == "remote:alive-laptop"
    assert LEASE_SECONDS >= 300
