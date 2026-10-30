from __future__ import annotations

import os

import pytest

from outpost import db, worker
from outpost.judge import Judge, ProblemSpec, token_compare
from outpost.sandbox import Limits, Sandbox

AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
AB_INT = "#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}"  # overflows
AB_PY = "a, b = map(int, input().split())\nprint(a + b)\n"
AB_JAVA = """import java.util.*;
public class Solution { public static void main(String[] x){ Scanner s=new Scanner(System.in);
 System.out.println(s.nextLong()+s.nextLong()); } }"""
TLE = "int main(){ volatile long long x=0; for(;;) x++; }"
SLEEP = "#include <unistd.h>\nint main(){ sleep(100); }"
MLE = "#include <bits/stdc++.h>\nint main(){ std::vector<long long> v(400000000, 1); std::cout<<v[123]; }"
RE = "int main(){ int *p = nullptr; return *p; }"
CE = "int main( { }"
GUESS = open(os.path.join(os.path.dirname(__file__), "packages.py")).read().split('GUESS_MAIN = r"""')[1].split('"""')[0]


def submit(conn, problem_id, source, language="cpp", contest_id=None):
    user = db.one(conn, "SELECT id FROM users WHERE username='judge-test'")
    if user is None:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES "
                           "('judge-test', 'x', 0)").lastrowid
    else:
        uid = user["id"]
    return conn.execute(
        "INSERT INTO submissions (contest_id, problem_id, user_id, language, source, submitted_at) "
        "VALUES (?,?,?,?,?,?)", (contest_id, problem_id, uid, language, source, db.now())).lastrowid


def judged(conn, settings, sub_id):
    worker.drain(settings)
    return db.one(conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))


@pytest.mark.parametrize("source,language,verdict", [
    (AB, "cpp", "accepted"),
    (AB_PY, "py12", "accepted"),
    (AB_JAVA, "java21", "accepted"),
    (AB_INT, "cpp", "wrong_answer"),
    (TLE, "cpp", "time_limit_exceeded"),
    (SLEEP, "cpp", "time_limit_exceeded"),
    (MLE, "cpp", "memory_limit_exceeded"),
    (RE, "cpp", "runtime_error"),
    (CE, "cpp", "compilation_error"),
    ("print(", "py12", "compilation_error"),
])
def test_verdicts(conn, settings, problems, source, language, verdict):
    sub = judged(conn, settings, submit(conn, problems["a-plus-b"], source, language))
    assert sub["status"] == "done"
    assert sub["verdict"] == verdict, sub["compile_output"]


def test_subtask_scores(conn, settings, problems):
    # Without a contest the judge runs IOI-style: AB_INT passes the samples and
    # group 1 (small numbers) but overflows on group 2's tests.
    sub = judged(conn, settings, submit(conn, problems["a-plus-b"], AB_INT))
    assert sub["score"] == 40 and sub["max_score"] == 100
    groups = {g["name"]: g for g in db.loads(sub["group_results_json"])}
    assert groups["1"]["passed"] and not groups["2"]["passed"]
    full = judged(conn, settings, submit(conn, problems["a-plus-b"], AB))
    assert full["score"] == 100 and full["verdict"] == "accepted"


def test_dependency_blocks_group(conn, settings, problems):
    # Fails the samples (group 0) -> group 1 depends on it and is skipped.
    wrong_small = ("#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;"
                   "std::cout<<(a+b==3?4:a+b);}")
    sub = judged(conn, settings, submit(conn, problems["a-plus-b"], wrong_small))
    groups = {g["name"]: g for g in db.loads(sub["group_results_json"])}
    assert groups["1"]["blocked_by"] == ["0"] and groups["1"]["score"] == 0
    assert groups["2"]["score"] == 60
    assert sub["score"] == 60


def test_interactive(conn, settings, problems):
    ok = judged(conn, settings, submit(conn, problems["guess"], GUESS))
    assert ok["verdict"] == "accepted", ok["test_results_json"]
    bad = judged(conn, settings, submit(conn, problems["guess"],
                 "#include <bits/stdc++.h>\nint main(){ std::cout << \"! 5\" << std::endl; }"))
    assert bad["verdict"] == "wrong_answer"


@pytest.mark.skipif(os.geteuid() != 0, reason="uid isolation needs root")
def test_submission_cannot_read_tests(conn, settings, problems):
    steal = ("#include <bits/stdc++.h>\nint main(){ std::ifstream f(\"%s\"); long long x;"
             " if(f >> x) std::cout << x; else return 1; }"
             % (settings.problems_dir / "a-plus-b" / "tests" / "01.a"))
    sub = judged(conn, settings, submit(conn, problems["a-plus-b"], steal))
    assert sub["verdict"] == "runtime_error"


def test_no_network(tmp_path):
    from outpost.sandbox import network_isolation_available
    if not network_isolation_available():
        pytest.skip("no network namespaces here")
    sb = Sandbox(tmp_path / "box", uid=None)
    sb.prepare()
    code = ("import socket,sys\ns=socket.socket(); s.settimeout(2)\n"
            "try:\n s.connect(('1.1.1.1',80)); sys.exit(0)\nexcept OSError: sys.exit(3)")
    res = sb.run(["python3", "-c", code], Limits(time_ms=5000, memory_mb=None))
    assert res.exit_code == 3


def test_token_compare(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("1  2\n3\n")
    b.write_text("1 2 3")
    assert token_compare(a, b)[0]
    b.write_text("1 2 4")
    ok, msg = token_compare(a, b)
    assert not ok and "token 3" in msg


def test_icpc_mode_stops_at_first_failure(settings, problems, conn):
    row = db.one(conn, "SELECT * FROM problems WHERE slug='a-plus-b'")
    spec: ProblemSpec = worker.spec_for(row, settings)
    j = worker.make_judge(settings, 5, False)
    out = j.judge(spec, "cpp", AB_INT, "icpc")
    assert out.verdict == "wrong_answer" and out.failed_test == 5
    assert [t.test for t in out.tests] == [1, 2, 3, 4, 5]
    assert isinstance(j, Judge)
