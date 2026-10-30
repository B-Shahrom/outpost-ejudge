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


def test_two_judges_cannot_share_a_box(settings):
    held = worker.lock_box(settings, 9)
    with pytest.raises(SystemExit, match="in use"):
        worker.lock_box(settings, 9)
    held.close()
    worker.lock_box(settings, 9).close()  # free again once released


def test_precompiled_header_is_used(settings, tmp_path):
    from outpost import languages
    pch = languages.prepare_pch(settings.pch_dir, log=lambda _: None)
    assert pch and (pch / "bits" / "stdc++.h.gch").exists()
    j = worker.make_judge(settings, 6, False)
    # -H lists every header g++ opens; "!" marks a precompiled one it used.
    probe = AB + "\n"
    ok, _, _ = j.compile(languages.get("cpp"), probe)
    assert ok
    import subprocess
    out = subprocess.run(["g++", "-I", str(pch), *languages.get("cpp").compile[1:-3], "-H", "-fsyntax-only",
                          "-x", "c++", "-"], input=probe, capture_output=True, text=True).stderr
    assert out.lstrip().startswith("! "), out[:200]
    j.sb.clean()


def test_selftest_passes(settings, capsys):
    from outpost import selftest
    assert selftest.run() == 0, capsys.readouterr().out
    assert capsys.readouterr().out.rstrip().endswith("Ready.")


def _build_pch(path):
    from outpost import languages
    return str(languages.prepare_pch(path, log=lambda _: None))


def test_precompiled_header_built_by_two_processes_at_once(tmp_path):
    # The start scripts start the server and run `outpost selftest` back to
    # back; both build the header on first start (this used to crash one).
    import multiprocessing as mp
    with mp.get_context("spawn").Pool(2) as pool:
        results = pool.map(_build_pch, [tmp_path / "pch"] * 2)
    assert results == [str(tmp_path / "pch")] * 2
    assert (tmp_path / "pch" / "bits" / "stdc++.h.gch").exists()
    assert not list((tmp_path / "pch" / "bits").glob("*.tmp"))


@pytest.mark.skipif(os.geteuid() != 0, reason="the filesystem jail needs root")
def test_filesystem_jail(settings, tmp_path):
    from outpost.sandbox import jail_available
    if not jail_available(settings.boxes_dir):
        pytest.skip("no mount namespaces here")
    sb = worker.make_judge(settings, 8, False, True).sb
    sb.prepare()
    probe = sb.box / "probe.py"
    probe.write_text(
        "import os\n"
        # The box lives under the data dir, so that path exists as a bare
        # skeleton; what it holds (problems, the database) must not.
        f"print('problems', os.path.exists({str(settings.problems_dir)!r}))\n"
        f"print('db', os.path.exists({str(settings.db_path)!r}))\n"
        f"print('listing', sorted(os.listdir({str(settings.data_dir)!r})))\n"
        "print('home', os.path.exists('/home'), 'root', os.path.exists('/root'))\n"
        "open('/tmp/scratch', 'w').write('x'); print('tmp ok')\n"
        "open('here.txt', 'w').write('x'); print('box ok')\n"
        "try:\n open('/usr/evil', 'w'); print('usr WRITABLE')\nexcept OSError: print('usr ro')\n"
        "try:\n open('/etc/evil', 'w'); print('etc WRITABLE')\nexcept OSError: print('etc ro')\n")
    sb.give(probe)
    out = tmp_path / "out"
    with open(out, "wb") as f:
        res = sb.run(["python3", "probe.py"], Limits(time_ms=5000, memory_mb=None), stdout=f)
    text = out.read_text()
    assert res.ok, text
    assert "problems False" in text and "db False" in text, text
    listing = eval(text.split("listing ")[1].split("\n")[0])
    assert set(listing) <= {"boxes", "pch", "runtime"}, listing  # only what runs need
    assert "home False root False" in text
    assert "tmp ok" in text and "box ok" in text and "usr ro" in text and "etc ro" in text
    assert (sb.box / "here.txt").exists()          # the box is the real one
    assert not any(str(sb.jail_root) in line for line in open("/proc/self/mounts"))  # nothing leaked
    sb.clean()


def test_memory_is_the_programs_not_the_judges(settings, tmp_path):
    # ru_maxrss survives exec, so forking from a big judge used to charge its
    # memory to the program (a correct A+B came back memory_limit_exceeded
    # when the judging process held 300 MB).
    ballast = bytearray(300 * 1024 * 1024)
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1
    sb = worker.make_judge(settings, 10, False, False).sb
    assert sb.launcher, "the launcher should build wherever gcc exists"
    sb.prepare()
    res = sb.run(["/bin/true"], Limits(time_ms=1000, memory_mb=256))
    assert res.ok and res.memory_kb < 20 * 1024, res
    hog = sb.run(["python3", "-c", "x = bytearray(150 * 1024 * 1024); x[::4096] = b'1' * len(x[::4096])"],
                 Limits(time_ms=5000, memory_mb=None))
    assert 150 * 1024 < hog.memory_kb < 250 * 1024, hog
    del ballast
    sb.clean()


@pytest.mark.skipif(os.geteuid() != 0, reason="a separate uid needs root")
def test_program_cannot_forge_the_launchers_report(settings, tmp_path):
    sb = worker.make_judge(settings, 11, False, False).sb
    sb.prepare()
    forge = sb.box / "forge.py"
    forge.write_text(
        "import os\n"
        "for fd in range(3, 20):\n"
        "    try: os.write(fd, b'0 0 1 1 1\\n'); print('wrote fd', fd)\n"
        "    except OSError: pass\n"
        "ppid = os.getppid()\n"
        "for fd in range(3, 20):\n"
        "    try:\n"
        "        with open(f'/proc/{ppid}/fd/{fd}', 'w') as f: f.write('0 0 1 1 1\\n'); print('proc fd', fd)\n"
        "    except OSError: pass\n"
        "x = 0\n"
        "for i in range(3 * 10**7): x += i\n")
    sb.give(forge)
    out = tmp_path / "out"
    with open(out, "wb") as f:
        res = sb.run(["python3", "forge.py"], Limits(time_ms=20000, memory_mb=None), stdout=f)
    assert "wrote" not in out.read_text() and "proc fd" not in out.read_text(), out.read_text()
    assert res.time_ms > 100, res   # the real CPU time, not the forged 0 ms
    sb.clean()


@pytest.mark.skipif(os.geteuid() != 0, reason="a separate uid needs root")
def test_fork_bomb_leaves_nothing_behind(settings, problems, conn):
    from outpost.sandbox import processes_of
    row = db.one(conn, "SELECT * FROM problems WHERE slug='a-plus-b'")
    spec = worker.spec_for(row, settings)
    j = worker.make_judge(settings, 14, *worker.protections(settings))
    bomb = "#include <unistd.h>\nint main(){ while(1) fork(); }"
    out = j.judge(spec, "cpp", bomb, "icpc")
    assert out.verdict in ("time_limit_exceeded", "runtime_error"), out.verdict
    assert processes_of(j.sb.uid) == []
    # It used to keep the uid at its process cap: the next run couldn't even compile.
    assert j.judge(spec, "cpp", AB, "icpc").verdict == "accepted"


@pytest.mark.skipif(os.geteuid() != 0, reason="a separate uid needs root")
def test_a_program_cannot_multiply_its_memory(settings, problems, conn):
    from outpost.sandbox import processes_of
    row = db.one(conn, "SELECT * FROM problems WHERE slug='a-plus-b'")
    spec = worker.spec_for(row, settings)
    j = worker.make_judge(settings, 15, *worker.protections(settings))
    forker = ("#include <bits/stdc++.h>\n#include <unistd.h>\n#include <sys/wait.h>\n"
              "int main(){ int made = 0; for (int i = 0; i < 40; i++) { pid_t p = fork(); if (p == 0) { pause(); }"
              " if (p > 0) made++; } long long a,b; std::cin>>a>>b; std::cout<<a+b; fprintf(stderr, \"made %d\", made); }")
    out = j.judge(spec, "cpp", forker, "icpc")
    assert out.verdict == "accepted"
    made = int(open(j.io / "err.txt").read().split()[-1])
    assert made <= 7, made          # the process cap held (8 with the launcher)
    assert processes_of(j.sb.uid) == []
