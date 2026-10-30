from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from outpost import auth, db, worker
from outpost.app import create_app

AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
AB_INT = "#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}"


@pytest.fixture(scope="module")
def app(settings, problems):
    c = db.connect(settings.db_path)
    if not db.one(c, "SELECT 1 FROM users WHERE username='jury'"):
        auth.create_user(c, "jury", "jurypass", is_admin=True)
    c.close()
    return create_app(settings)


def client(app, username=None, password=None) -> TestClient:
    cl = TestClient(app, follow_redirects=False)
    if username:
        r = cl.post("/login", data={"username": username, "password": password})
        assert r.status_code == 303, r.text
    return cl


def make_contest(admin: TestClient, code: str, scoring: str, *, start_offset_min=-10,
                 duration=120, freeze=0, divisions="") -> None:
    start = datetime.fromtimestamp(time.time() + start_offset_min * 60).strftime("%Y-%m-%dT%H:%M")
    r = admin.post("/admin/contests", data={
        "code": code, "name": f"Contest {code}", "start": start, "duration": duration,
        "scoring": scoring, "penalty": 20, "freeze": freeze, "divisions": divisions,
        "visible": "1", "public_standings": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/admin/contests/{code}"


def add_problem(admin, code, problem_id, label=""):
    r = admin.post(f"/admin/contests/{code}/problems", data={"problem_id": problem_id, "label": label})
    assert r.status_code == 303


def make_users(admin, lines: str) -> None:
    assert admin.post("/admin/users/import", data={"rows": lines}).status_code == 303


def test_login_required(app):
    r = client(app).get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client(app).post("/login", data={"username": "jury", "password": "nope"}).headers["location"] == "/login"


def test_admin_pages_render(app, problems):
    admin = client(app, "jury", "jurypass")
    for url in ("/admin", "/admin/problems", f"/admin/problems/{problems['a-plus-b']}",
                "/admin/contests", "/admin/users", "/admin/submissions"):
        r = admin.get(url)
        assert r.status_code == 200, url
    assert "a-plus-b" in admin.get("/admin/problems").text


def test_icpc_contest_end_to_end(app, settings, problems):
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "icpc1", "icpc")
    add_problem(admin, "icpc1", problems["a-plus-b"])
    add_problem(admin, "icpc1", problems["guess"])
    make_users(admin, "ann, Ann A, Div A, pw-ann\nbob, Bob B, Div A, pw-bob")

    ann = client(app, "ann", "pw-ann")
    # One running contest -> straight into it.
    assert ann.get("/").headers["location"] == "/c/icpc1"
    page = ann.get("/c/icpc1/p/A")
    assert page.status_code == 200 and "their sum" in page.text and r"\(a\)" in page.text
    assert ann.get("/c/icpc1/p/B").status_code == 200

    r = ann.post("/c/icpc1/submit", data={"label": "A", "language": "cpp", "source": AB_INT})
    assert r.headers["location"] == "/c/icpc1/submissions"
    # Cooldown between submissions.
    ann.post("/c/icpc1/submit", data={"label": "A", "language": "cpp", "source": AB})
    worker.drain(settings)
    subs = ann.get("/api/c/icpc1/my-submissions").json()
    assert len(subs) == 1 and subs[0]["verdict"] == "wrong_answer" and subs[0]["failed_test"] == 5

    c = db.connect(settings.db_path)
    c.execute("UPDATE submissions SET submitted_at = submitted_at - 60")  # skip the cooldown
    ann.post("/c/icpc1/submit", files={"file": ("sol.cpp", AB.encode())}, data={"label": "A", "language": "py12"})
    worker.drain(settings)
    subs = ann.get("/api/c/icpc1/my-submissions").json()
    assert subs[0]["verdict"] == "accepted"
    assert db.one(c, "SELECT language FROM submissions WHERE id=?", (subs[0]["id"],))["language"] == "cpp"

    board = ann.get("/c/icpc1/standings").text
    assert "Ann A" in board and "Bob B" in board
    # Submission detail: own only.
    sid = subs[0]["id"]
    assert ann.get(f"/c/icpc1/s/{sid}").status_code == 200
    bob = client(app, "bob", "pw-bob")
    assert bob.get(f"/c/icpc1/s/{sid}").status_code == 404

    # Admin sees it, can rejudge it.
    assert admin.get(f"/admin/submissions/{sid}").status_code == 200
    admin.post("/admin/rejudge", data={"id": sid})
    assert db.one(c, "SELECT status FROM submissions WHERE id=?", (sid,))["status"] == "pending"
    worker.drain(settings)
    assert db.one(c, "SELECT verdict FROM submissions WHERE id=?", (sid,))["verdict"] == "accepted"

    csv_text = admin.get("/admin/contests/icpc1/standings.csv").text
    assert csv_text.splitlines()[1].startswith("1,ann,")


def test_single_session(app):
    admin = client(app, "jury", "jurypass")
    make_users(admin, "solo, Solo, , pw-solo")
    first = client(app, "solo", "pw-solo")
    assert first.get("/c/icpc1/standings").status_code == 200
    client(app, "solo", "pw-solo")  # signs in elsewhere
    assert first.get("/c/icpc1/standings").headers["location"] == "/login"


def test_not_started_hides_problems(app, problems):
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "later", "icpc", start_offset_min=60)
    add_problem(admin, "later", problems["a-plus-b"])
    make_users(admin, "early, Early, , pw-early")
    early = client(app, "early", "pw-early")
    assert early.get("/c/later/p/A").headers["location"] == "/c/later"
    r = early.post("/c/later/submit", data={"label": "A", "language": "cpp", "source": AB})
    assert r.headers["location"] == "/c/later"


def test_divisions(app, problems):
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "div-only", "ioi", divisions="Div X")
    add_problem(admin, "div-only", problems["a-plus-b"])
    make_users(admin, "x1, X One, Div X, pw-x1\ny1, Y One, Div Y, pw-y1")
    assert client(app, "x1", "pw-x1").get("/c/div-only").status_code == 200
    assert client(app, "y1", "pw-y1").get("/c/div-only").status_code == 404
    board = admin.get("/c/div-only/standings").text
    assert "X One" in board and "Y One" not in board


def test_clarifications(app):
    admin = client(app, "jury", "jurypass")
    ann = client(app, "ann", "pw-ann")
    ann.post("/c/icpc1/clarifications", data={"question": "Is a negative?", "label": "A"})
    page = admin.get("/admin/contests/icpc1/clarifications").text
    assert "Is a negative?" in page
    c = db.connect(app.state.settings.db_path)
    cid = db.one(c, "SELECT id FROM clarifications WHERE question='Is a negative?'")["id"]
    admin.post("/admin/contests/icpc1/clarifications", data={"cid": cid, "answer": "No.", "public": "1"})
    admin.post("/admin/contests/icpc1/clarifications", data={"answer": "Problem B fixed."})
    bob = client(app, "bob", "pw-bob")
    text = bob.get("/c/icpc1/clarifications").text
    assert "Is a negative?" in text and "No." in text and "Problem B fixed." in text
    assert bob.get("/api/c/icpc1/news").json()["latest"] > 0


def test_slips_and_password_reset(app):
    admin = client(app, "jury", "jurypass")
    make_users(admin, "gen1, Generated One, Div A")
    page = admin.get("/admin/users").text
    assert "gen1" in page and "Print login slips" in page
    batch = page.split("/admin/slips/")[1].split('"')[0]
    slips = admin.get(f"/admin/slips/{batch}").text
    assert "gen1" in slips
    admin.post("/admin/forget-passwords")
    assert "Print login slips" not in admin.get("/admin/users").text


def test_statement_files_need_access(app, settings, problems):
    target = settings.problems_dir / "a-plus-b" / "statements" / "english"
    (target / "pic.png").write_bytes(b"\x89PNG fake")
    ann = client(app, "ann", "pw-ann")
    assert ann.get("/problem-files/a-plus-b/english/pic.png").status_code == 200
    assert ann.get("/problem-files/a-plus-b/english/..%2F..%2Ftests%2F01.a").status_code == 404
    make_users(client(app, "jury", "jurypass"), "outsider, Out, Div Z, pw-out")
    # Div Z sees icpc1 (open to all) — but not a problem that only lives in a hidden contest.
    assert client(app, "outsider", "pw-out").get("/problem-files/guess/english/x.png").status_code == 404


def test_upload_import_runs_in_background(app, settings, tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import packages
    admin = client(app, "jury", "jurypass")
    z = packages.a_plus_b(tmp_path / "ab.zip")
    r = admin.post("/admin/problems/import", files={"package": ("ab.zip", z.read_bytes())})
    assert r.status_code == 303
    job = next(iter(app.state.imports.values()))
    job["proc"].wait(120)
    page = admin.get("/admin/problems").text
    assert "imported" in page, page


def test_contestant_address_can_be_set(app):
    from outpost.config import normalise_url
    assert normalise_url("192.168.137.1:8000") == "http://192.168.137.1:8000/"
    assert normalise_url(" ") == ""
    admin = client(app, "jury", "jurypass")
    admin.post("/admin/address", data={"url": "192.168.137.1:8000"})
    page = admin.get("/admin").text
    assert "http://192.168.137.1:8000/" in page and "<svg" in page
    make_users(admin, "slip1, Slip One, Div A")
    batch = admin.get("/admin/users").text.split("/admin/slips/")[1].split('"')[0]
    assert "http://192.168.137.1:8000/" in admin.get(f"/admin/slips/{batch}").text
    admin.post("/admin/address", data={"url": ""})
    assert "Detected" in admin.get("/admin").text or "container" in admin.get("/admin").text


def test_mdns_name_is_offered_after_the_ip(app):
    admin = client(app, "jury", "jurypass")
    admin.post("/admin/address", data={"url": "192.168.137.1:8000"})
    app.state.mdns_host = "outpost.local"
    try:
        page = admin.get("/admin").text
        assert page.index("http://192.168.137.1:8000/") < page.index("http://outpost.local:8000/")
        make_users(admin, "mdns1, M One, Div A")
        batch = admin.get("/admin/users").text.split("/admin/slips/")[1].split('"')[0]
        assert "(or http://outpost.local:8000/)" in admin.get(f"/admin/slips/{batch}").text
    finally:
        app.state.mdns_host = None
        admin.post("/admin/address", data={"url": ""})


def test_print_view_and_merged_pdf(app, settings, problems):
    import io

    from pypdf import PdfReader, PdfWriter
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "print1", "icpc")
    add_problem(admin, "print1", problems["a-plus-b"])
    add_problem(admin, "print1", problems["guess"])
    page = admin.get("/c/print1/print").text
    assert page.index("A. A + B") < page.index("B. Guess the Number")
    assert page.count('class="panel statement print-problem"') == 2
    # Not every problem has a PDF: no merge, only the print view.
    assert admin.get("/c/print1/statements.pdf").status_code == 404
    make_contest(admin, "print2", "icpc")
    add_problem(admin, "print2", problems["a-plus-b"], "A")
    pdf = settings.problems_dir / "a-plus-b" / "statements" / "english" / "problem.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    w.write(str(pdf))
    try:
        r = admin.get("/c/print2/statements.pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert len(PdfReader(io.BytesIO(r.content)).pages) == 2
    finally:
        pdf.unlink()


def test_archive_download_and_upload(app):
    admin = client(app, "jury", "jurypass")
    r = admin.get("/admin/contests/icpc1/archive.zip")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    up = admin.post("/admin/archive/import", files={"archive": ("a.zip", r.content)}, data={"code": "icpc1-copy"})
    assert up.headers["location"] == "/admin/contests/icpc1-copy"
    assert "Contest icpc1-copy" in admin.get("/admin/contests/icpc1-copy").text or \
        "icpc1-copy" in admin.get("/admin/contests/icpc1-copy").text
    again = admin.post("/admin/archive/import", files={"archive": ("a.zip", r.content)}, data={"code": "icpc1-copy"})
    assert again.headers["location"] == "/admin/contests"
