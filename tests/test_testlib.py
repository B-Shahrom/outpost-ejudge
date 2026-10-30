"""Import and judge packages built the way Polygon builds them, with the real testlib."""
from __future__ import annotations

import pytest

import real_packages as rp
from outpost import db, polygon, worker

SUM = rp.SUM_MAIN
SUM_INT = rp.SUM_WA                                      # overflows on the big group
SUM_EXTRA = SUM.replace('cout << s << "\\n";', 'cout << s << " " << 1 << "\\n";')
SUM_NOTHING = "int main(){}"
OFF_BY_ONE = SUM.replace("cout << s", "cout << s + 1")
AB_SILENT = "#include <bits/stdc++.h>\nint main(){ long long a,b; std::cin>>a>>b; }"


@pytest.fixture(scope="module")
def real(settings, tmp_path_factory):
    d = tmp_path_factory.mktemp("real")
    out = {}
    for build in (rp.standard_package, rp.points_package, rp.interactive_package):
        rec = polygon.Recorder()
        pid = polygon.import_package(build(d / f"{build.__name__}.zip"), settings, log=rec)
        out[build.__name__] = (pid, rec.lines)
    return out


def run(conn, settings, problem_id, source, contest_mode=None):
    """Judge ``source``; ``contest_mode='icpc'`` puts it in an ICPC contest."""
    contest_id = None
    if contest_mode:
        row = db.one(conn, "SELECT id FROM contests WHERE code='testlib-icpc'")
        contest_id = row["id"] if row else conn.execute(
            "INSERT INTO contests (code, name, start_at, end_at, scoring, visible, created_at) "
            "VALUES ('testlib-icpc', 'x', 0, 1e12, 'icpc', 0, 0)").lastrowid  # hidden: other tests count running contests
    user = db.one(conn, "SELECT id FROM users WHERE username='testlib-user'")
    uid = user["id"] if user else conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES ('testlib-user','x',0)").lastrowid
    sid = conn.execute("INSERT INTO submissions (contest_id, problem_id, user_id, language, source, "
                       "submitted_at) VALUES (?,?,?,?,?,?)",
                       (contest_id, problem_id, uid, "cpp", source, db.now())).lastrowid
    worker.drain(settings)
    return db.one(conn, "SELECT * FROM submissions WHERE id=?", (sid,))


def test_standard_package_imports(real, conn, settings):
    pid, log = real["standard_package"]
    row = db.one(conn, "SELECT * FROM problems WHERE id=?", (pid,))
    assert row["checker"] == "testlib" and row["slug"] == "array-sum"
    assert "generated 3 test(s)" in log and "computed 5 answer(s) with the main solution" in log
    assert any(line.startswith("validator: all 5 tests valid") for line in log), log
    big = settings.problems_dir / "array-sum" / "tests" / "05"
    assert big.read_text().split("\n")[0] == "100000"
    st = db.loads(row["statements_json"])
    assert set(st) == {"english", "russian"} and st["english"]["pdf"]
    assert r"\(1 \le n \le 10^5\)" in st["english"]["input"]


def test_wcmp_verdicts(real, conn, settings):
    pid = real["standard_package"][0]
    assert run(conn, settings, pid, SUM)["verdict"] == "accepted"
    wa = run(conn, settings, pid, OFF_BY_ONE, "icpc")
    assert wa["verdict"] == "wrong_answer" and wa["failed_test"] == 1
    assert "expected" in db.loads(wa["test_results_json"])[0]["message"]
    # wcmp stops at the answer's last token: extra output is a wrong answer.
    assert run(conn, settings, pid, SUM_EXTRA, "icpc")["verdict"] == "wrong_answer"
    # No output at all: testlib's unexpected EOF.
    assert run(conn, settings, pid, SUM_NOTHING, "icpc")["verdict"] == "wrong_answer"


def test_groups_with_real_checker(real, conn, settings):
    pid = real["standard_package"][0]
    sub = run(conn, settings, pid, SUM_INT)
    groups = {g["name"]: g for g in db.loads(sub["group_results_json"])}
    assert groups["1"]["passed"] and not groups["2"]["passed"]
    assert sub["score"] == 30 and sub["max_score"] == 100


def test_partial_points_from_quitp(real, conn, settings):
    pid = real["points_package"][0]
    exact = run(conn, settings, pid, SUM)
    assert exact["score"] == 20 and exact["verdict"] == "accepted"
    close = run(conn, settings, pid, OFF_BY_ONE)
    tests = db.loads(close["test_results_json"])
    assert [t["points"] for t in tests[1:]] == [5.0, 5.0], tests
    assert close["score"] == 10 and close["verdict"] == "wrong_answer"


def test_testlib_interactor(real, conn, settings):
    pid = real["interactive_package"][0]
    assert (settings.problems_dir / "ab-interactive" / "tests" / "01.a").read_text().split() == ["3", "7"]
    assert run(conn, settings, pid, rp.AB_SOLUTION, "icpc")["verdict"] == "accepted"
    assert run(conn, settings, pid, AB_SILENT, "icpc")["verdict"] == "wrong_answer"
    wrong = rp.AB_SOLUTION.replace("a + b", "a - b")
    assert run(conn, settings, pid, wrong, "icpc")["verdict"] == "wrong_answer"
