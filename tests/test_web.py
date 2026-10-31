from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import real_packages as rp
from outpost import auth, db, polygon, worker
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


def test_standings_are_computed_once_per_change(app, settings, problems, monkeypatch):
    from outpost import scoring
    computed = []
    real = scoring.standings
    monkeypatch.setattr(scoring, "standings", lambda *a, **k: computed.append(1) or real(*a, **k))
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "cache1", "icpc")
    add_problem(admin, "cache1", problems["a-plus-b"])
    make_users(admin, "cara, Cara C, , pw-cara")
    cara = client(app, "cara", "pw-cara")

    def solved() -> int:
        page = cara.get("/c/cache1/standings").text
        return page.count('class="cell-first"') + page.count('class="cell-ok"')

    assert solved() == 0
    n = len(computed)
    assert solved() == 0 and cara.get("/c/cache1/standings").status_code == 200
    assert len(computed) == n                       # nothing changed: the board is reused
    cara.post("/c/cache1/submit", data={"label": "A", "language": "cpp", "source": AB})
    worker.drain(settings)
    assert solved() == 1                            # a judged submission shows at once
    sid = cara.get("/api/c/cache1/my-submissions").json()[0]["id"]
    admin.post("/admin/rejudge", data={"id": sid})
    assert solved() == 0                            # and so does a rejudge (pending again)
    worker.drain(settings)
    assert solved() == 1
    add_problem(admin, "cache1", problems["guess"])
    assert ">B</th>" in cara.get("/c/cache1/standings").text  # admin changes show at once


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


@pytest.fixture(scope="module")
def pdf_problems(settings, tmp_path_factory):
    """Polygon packages with a statement PDF: two that open, one truncated. {slug: (id, log)}"""
    d = tmp_path_factory.mktemp("pdfs")
    good = rp.tiny_pdf()
    out = {}
    for slug, pdf in (("pdf-a", good), ("pdf-b", good), ("pdf-broken", good[:len(good) // 2])):
        rec = polygon.Recorder()
        pid = polygon.import_package(rp.points_package(d / f"{slug}.zip", slug, pdf), settings,
                                     log=rec, verify=False)
        out[slug] = (pid, rec.lines)
    return out


def test_broken_statement_pdf_is_dropped_on_import(app, settings, conn, pdf_problems):
    pid, log = pdf_problems["pdf-broken"]
    assert any(line.startswith("WARNING statement PDF statements/.pdf/english/problem.pdf dropped: "
                               "it doesn't parse") for line in log), log
    row = db.one(conn, "SELECT * FROM problems WHERE id=?", (pid,))
    assert row["status"] == "ready" and row["checker"] == "testlib"
    assert not db.loads(row["statements_json"])["english"]["pdf"]
    assert not (settings.problems_dir / "pdf-broken" / "statements" / "english" / "problem.pdf").exists()

    admin = client(app, "jury", "jurypass")
    make_contest(admin, "pdf1", "icpc")
    add_problem(admin, "pdf1", pdf_problems["pdf-a"][0], "A")
    add_problem(admin, "pdf1", pid, "B")
    assert 'problem.pdf" target="_blank">PDF</a>' in admin.get("/c/pdf1/p/A").text
    page = admin.get("/c/pdf1/p/B")
    assert page.status_code == 200 and "Sum of the Array" in page.text and "problem.pdf" not in page.text
    assert admin.get("/problem-files/pdf-broken/english/problem.pdf").status_code == 404
    # Not every problem has a PDF: no merge offered.
    assert "Package PDFs, merged" not in admin.get("/c/pdf1/print").text
    assert admin.get("/c/pdf1/statements.pdf").status_code == 404


def test_merge_with_a_pdf_that_broke_after_import(app, settings, pdf_problems):
    """Like a problem imported before imports checked PDFs: the statement says it has one,
    and the file doesn't open."""
    import io

    from pypdf import PdfReader
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "pdf2", "icpc")
    add_problem(admin, "pdf2", pdf_problems["pdf-a"][0], "A")
    add_problem(admin, "pdf2", pdf_problems["pdf-b"][0], "B")
    assert "Package PDFs, merged" in admin.get("/c/pdf2/print").text
    r = admin.get("/c/pdf2/statements.pdf")
    assert r.status_code == 200 and len(PdfReader(io.BytesIO(r.content)).pages) == 2

    pdf = settings.problems_dir / "pdf-b" / "statements" / "english" / "problem.pdf"
    original = pdf.read_bytes()
    pdf.write_bytes(b"%PDF-1.4 fake\n%%EOF\n")
    try:
        assert "problem.pdf" not in admin.get("/c/pdf2/p/B").text
        assert 'problem.pdf" target="_blank">PDF</a>' in admin.get("/c/pdf2/p/A").text
        assert admin.get("/problem-files/pdf-b/english/problem.pdf").status_code == 404
        assert "Package PDFs, merged" not in admin.get("/c/pdf2/print").text
        assert admin.get("/c/pdf2/statements.pdf").status_code == 404  # not a 500
    finally:
        pdf.write_bytes(original)
    assert admin.get("/c/pdf2/statements.pdf").status_code == 200
    assert admin.get("/problem-files/pdf-b/english/problem.pdf").status_code == 200


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


def test_resolver_page_rules(app):
    admin = client(app, "jury", "jurypass")
    assert "no freeze" in admin.get("/admin/contests/icpc1/resolver").text
    assert "IOI" in admin.get("/admin/contests/div-only/resolver").text
    ann = client(app, "ann", "pw-ann")
    assert ann.get("/admin/contests/icpc1/resolver.json").status_code == 403
    data = admin.get("/admin/contests/icpc1/resolver.json").json()
    assert {"problems", "rows", "reveal", "penalty_minutes"} <= set(data)


def test_analysis_page(app, settings, problems):
    admin = client(app, "jury", "jurypass")
    make_contest(admin, "anal", "ioi")
    add_problem(admin, "anal", problems["a-plus-b"])
    make_users(admin, "cop1, Copy One, , pw-c1\ncop2, Copy Two, , pw-c2")
    c = db.connect(settings.db_path)
    cid = db.one(c, "SELECT id FROM contests WHERE code='anal'")["id"]
    renamed = AB.replace("a,b", "x,y").replace(">>a>>b", ">>x>>y").replace("<<a+b", "<<x+y") + " // mine"
    for user, src in (("cop1", AB + "\n// pad pad pad\nstruct Z{int a,b,c;}; Z z;"),
                      ("cop2", renamed + "\nstruct Z{int a,b,c;}; Z z;")):
        uid = db.one(c, "SELECT id FROM users WHERE username=?", (user,))["id"]
        c.execute("INSERT INTO submissions (contest_id, problem_id, user_id, language, source, submitted_at) "
                  "VALUES (?,?,?,?,?,?)", (cid, problems["a-plus-b"], uid, "cpp", src, time.time()))
    worker.drain(settings)
    page = admin.get("/admin/contests/anal/analysis?threshold=0.5").text
    assert "cop1" in page and "cop2" in page and "/admin/compare/" in page
    assert 'class="heat-cell' in page
    link = page.split('href="/admin/compare/')[1].split('"')[0]
    assert "diff" in admin.get(f"/admin/compare/{link}").text


def test_team_contest(app, settings, problems):
    admin = client(app, "jury", "jurypass")
    start = datetime.fromtimestamp(time.time() - 600).strftime("%Y-%m-%dT%H:%M")
    admin.post("/admin/contests", data={"code": "teams1", "name": "Team Cup", "start": start, "duration": 120,
                                        "scoring": "icpc", "penalty": 20, "freeze": 0, "divisions": "TeamDiv",
                                        "visible": "1", "public_standings": "1", "team_mode": "1"})
    add_problem(admin, "teams1", problems["a-plus-b"])
    make_users(admin, "tm1, Team Mate 1, TeamDiv, pw, Rockets\ntm2, Team Mate 2, TeamDiv, pw, Rockets\n"
                      "solo2, Solo Two, TeamDiv, pw")
    one, two, solo = client(app, "tm1", "pw"), client(app, "tm2", "pw"), client(app, "solo2", "pw")
    one.post("/c/teams1/submit", data={"label": "A", "language": "cpp", "source": AB})
    worker.drain(settings)
    sid = one.get("/api/c/teams1/my-submissions").json()[0]["id"]
    assert two.get("/api/c/teams1/my-submissions").json()[0]["id"] == sid   # shared with the teammate
    assert two.get(f"/c/teams1/s/{sid}").status_code == 200                  # and so is the code
    assert solo.get(f"/c/teams1/s/{sid}").status_code == 404
    assert "Team submissions" in two.get("/c/teams1/submissions").text
    board = solo.get("/c/teams1/standings").text
    assert "Rockets" in board and "Team Mate 1, Team Mate 2" in board and "Solo Two" in board


def test_balloons_and_print_requests(app, settings, problems):
    admin = client(app, "jury", "jurypass")
    start = datetime.fromtimestamp(time.time() - 600).strftime("%Y-%m-%dT%H:%M")
    admin.post("/admin/contests", data={"code": "onsite", "name": "Onsite", "start": start, "duration": 120,
                                        "scoring": "icpc", "penalty": 20, "freeze": 0, "divisions": "Onsite",
                                        "visible": "1", "public_standings": "1", "team_mode": "1"})
    add_problem(admin, "onsite", problems["a-plus-b"])
    make_users(admin, "on1, On One, Onsite, pw, Comets, A-12\non2, On Two, Onsite, pw, Comets, A-13\n"
                      "on3, On Three, Onsite, pw, , B-01")
    one, two, three = client(app, "on1", "pw"), client(app, "on2", "pw"), client(app, "on3", "pw")
    for cl in (one, two, three):   # two Comets solve A: still one balloon for the team
        cl.post("/c/onsite/submit", data={"label": "A", "language": "cpp", "source": AB})
    worker.drain(settings)
    page = admin.get("/admin/contests/onsite/balloons").text
    assert "To deliver (2)" in page and "Comets" in page and "A-12, A-13" in page and "B-01" in page
    assert "A</b> red" in page and "★" in page
    c = db.connect(settings.db_path)
    pid = problems["a-plus-b"]
    admin.post("/admin/contests/onsite/balloons", data={"key": "t:Comets", "problem_id": pid})
    assert "To deliver (1)" in admin.get("/admin/contests/onsite/balloons").text
    admin.post("/admin/contests/onsite/balloons", data={"key": "t:Comets", "problem_id": pid, "undo": "1"})
    assert "To deliver (2)" in admin.get("/admin/contests/onsite/balloons").text

    # Printing is off until the admin allows it.
    assert one.get("/c/onsite/printer").status_code == 404
    cid = db.one(c, "SELECT id FROM contests WHERE code='onsite'")["id"]
    c.execute("UPDATE contests SET allow_print=1 WHERE id=?", (cid,))
    assert ">Print<" in one.get("/c/onsite/standings").text
    one.post("/c/onsite/printer", data={"text": "int main() { return 0; }  // please print"})
    r = one.post("/c/onsite/printer", data={"text": "again"})   # cooldown
    assert db.one(c, "SELECT COUNT(*) AS n FROM print_jobs WHERE contest_id=?", (cid,))["n"] == 1
    queue = admin.get("/admin/contests/onsite/prints").text
    assert "On One" in queue and "A-12" in queue and "Comets" in queue
    jid = db.one(c, "SELECT id FROM print_jobs WHERE contest_id=?", (cid,))["id"]
    job = admin.get(f"/admin/print-jobs/{jid}").text
    assert "please print" in job and "seat <b>A-12</b>" in job
    admin.post(f"/admin/print-jobs/{jid}/done")
    assert "printed" in one.get("/c/onsite/printer").text
    assert two.get(f"/admin/print-jobs/{jid}").status_code == 403


def test_problem_page_has_the_offline_editor(app):
    ann = client(app, "ann", "pw-ann")
    page = ann.get("/c/icpc1/p/A").text
    assert "data-editor" in page and 'data-draft-key="outpost-draft-icpc1-A"' in page
    for asset in ("/static/vendor/codemirror/codemirror.js", "/static/vendor/codemirror/clike.js",
                  "/static/vendor/codemirror/python.js", "/static/editor.js",
                  "/static/vendor/codemirror/codemirror.css"):
        assert asset in page and ann.get(asset).status_code == 200, asset
    assert "https://" not in page.split("<main>")[0]  # nothing from the internet in the head
