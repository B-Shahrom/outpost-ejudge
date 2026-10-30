"""Judge one submission against one problem.

The judge works on a *problem directory* laid out by the importer::

    <problem>/tests/01, 01.a, 02, 02.a, ...   inputs and answers
    <problem>/check                            testlib checker (optional)
    <problem>/interactor                       testlib interactor (interactive only)

and a pair of scratch directories per worker: ``box`` (owned by the sandbox
uid — the submission compiles and runs there) and ``io`` (ours — stdout and
stderr of each run land here, out of the submission's reach).

Verdict names match electicode's: accepted, wrong_answer, time_limit_exceeded,
memory_limit_exceeded, runtime_error, compilation_error, judge_error.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import languages
from .sandbox import Limits, RunResult, Sandbox

ACCEPTED = "accepted"
WRONG_ANSWER = "wrong_answer"
TIME_LIMIT = "time_limit_exceeded"
MEMORY_LIMIT = "memory_limit_exceeded"
RUNTIME_ERROR = "runtime_error"
COMPILATION_ERROR = "compilation_error"
JUDGE_ERROR = "judge_error"
SKIPPED = "skipped"

VERDICTS = (ACCEPTED, WRONG_ANSWER, TIME_LIMIT, MEMORY_LIMIT, RUNTIME_ERROR,
            COMPILATION_ERROR, JUDGE_ERROR)

_RUN_TO_VERDICT = {"tle": TIME_LIMIT, "mle": MEMORY_LIMIT, "re": RUNTIME_ERROR, "ole": RUNTIME_ERROR}

COMPILE_LIMITS = dict(time_ms=30_000, wall_ms=60_000, memory_mb=2048, output_mb=256, processes=64)
CHECKER_TIME_MS = 10_000
MESSAGE_CAP = 1000


@dataclass
class ProblemSpec:
    """What the judge needs to know about a problem (a ``problems`` row, unpacked)."""
    dir: Path
    time_limit_ms: int
    memory_limit_mb: int
    tests: list[dict]                       # [{index, group, points, sample}]
    groups: list[dict] = field(default_factory=list)  # [{name, points, policy, dependencies}]
    interactive: bool = False
    checker: str = "builtin"               # builtin | testlib

    def input(self, i: int) -> Path:
        return self.dir / "tests" / f"{i:02d}"

    def answer(self, i: int) -> Path:
        return self.dir / "tests" / f"{i:02d}.a"


@dataclass
class TestResult:
    test: int
    verdict: str
    time_ms: int = 0
    memory_kb: int = 0
    points: float | None = None
    message: str = ""


@dataclass
class Outcome:
    verdict: str
    score: float = 0.0
    max_score: float = 0.0
    time_ms: int = 0
    memory_kb: int = 0
    failed_test: int | None = None
    compile_output: str = ""
    tests: list[TestResult] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)

    def test_dicts(self) -> list[dict]:
        return [asdict(t) for t in self.tests]


class Judge:
    def __init__(self, sandbox: Sandbox, io_dir: Path):
        self.sb = sandbox
        self.io = io_dir

    # -- setup ------------------------------------------------------------
    def _reset_io(self) -> None:
        if self.io.exists():
            shutil.rmtree(self.io)
        self.io.mkdir(parents=True)
        self.io.chmod(0o700)

    def compile(self, lang: languages.Language, source: str) -> tuple[bool, str, list[str]]:
        """Write ``source`` into a fresh box and build it. Returns (ok, log, run argv)."""
        self.sb.prepare()
        self._reset_io()
        name, run = lang.source_name, list(lang.run)
        if lang.id == "java21":
            # Accept any public class name, not just Main.
            m = re.search(r"public\s+(?:final\s+)?class\s+([A-Za-z_]\w*)", source)
            if m:
                name = f"{m.group(1)}.java"
                run = [a if a != "Main" else m.group(1) for a in run]
        src = self.sb.box / name
        src.write_text(source, encoding="utf-8")
        self.sb.give(src)
        if not lang.compile:
            if lang.id == "py12":
                return self._python_syntax_check(src, lang, run)
            return True, "", run
        argv = [a if a != lang.source_name else name for a in lang.compile]
        log = self.io / "compile.txt"
        res = self.sb.run(argv, Limits(address_space=lang.address_space, **COMPILE_LIMITS),
                          stderr_path=log)
        text = _read_capped(log, 10_000)
        if not res.ok:
            if res.status == "tle":
                text = (text + "\ncompilation timed out").strip()
            return False, text, run
        return True, text, run

    def _python_syntax_check(self, src: Path, lang: languages.Language, run: list[str]):
        log = self.io / "compile.txt"
        argv = [lang.run[0], "-c", "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])", src.name]
        res = self.sb.run(argv, Limits(**COMPILE_LIMITS), stderr_path=log)
        return res.ok, _read_capped(log, 10_000), run

    # -- one test ---------------------------------------------------------
    def run_test(self, p: ProblemSpec, lang: languages.Language, run: list[str], i: int,
                 output_to: Path | None = None) -> TestResult:
        tl = int(p.time_limit_ms * lang.time_factor)
        limits = Limits(time_ms=tl, memory_mb=p.memory_limit_mb, address_space=lang.address_space,
                        rss_slack_mb=lang.rss_slack_mb, processes=lang.processes)
        argv = [a.replace("{mem}", str(p.memory_limit_mb)) for a in run]
        out = output_to or self.io / "out.txt"
        if p.interactive:
            return self._run_interactive(p, argv, limits, i, out)
        with open(p.input(i), "rb") as fin, open(out, "wb") as fout:
            res = self.sb.run(argv, limits, stdin=fin, stdout=fout, stderr_path=self.io / "err.txt")
        if not res.ok:
            return _from_run(i, res)
        if output_to is not None:  # generating answers: nothing to check against
            return TestResult(i, ACCEPTED, res.time_ms, res.memory_kb)
        verdict, points, msg = self.check(p, i, out)
        return TestResult(i, verdict, res.time_ms, res.memory_kb, points, msg)

    def _run_interactive(self, p: ProblemSpec, argv: list[str], limits: Limits, i: int,
                         tout: Path) -> TestResult:
        to_sol_r, to_sol_w = os.pipe()
        from_sol_r, from_sol_w = os.pipe()
        ilimits = Limits(time_ms=max(CHECKER_TIME_MS, limits.time_ms * 2), memory_mb=None,
                         wall_ms=int(limits.wall * 1000) + 2000)
        tout.write_bytes(b"")
        inter = self.sb.spawn([str(p.dir / "interactor"), str(p.input(i)), str(tout)], ilimits,
                              stdin=from_sol_r, stdout=to_sol_w, trusted=True,
                              stderr_path=self.io / "interactor.txt", cwd=self.io)
        sol = self.sb.spawn(argv, limits, stdin=to_sol_r, stdout=from_sol_w,
                            stderr_path=self.io / "err.txt")
        for fd in (to_sol_r, to_sol_w, from_sol_r, from_sol_w):
            os.close(fd)
        sres, ires = self.sb.wait(sol, inter)
        imsg = _read_capped(self.io / "interactor.txt", MESSAGE_CAP)
        if ires.exit_code == 3:
            return TestResult(i, JUDGE_ERROR, sres.time_ms, sres.memory_kb, None, "interactor: " + imsg)
        if sres.status in ("tle", "mle"):
            return _from_run(i, sres)
        if ires.exit_code in (1, 2):
            return TestResult(i, WRONG_ANSWER, sres.time_ms, sres.memory_kb, None, imsg)
        if not sres.ok:
            return _from_run(i, sres)
        if ires.status != "ok":
            return TestResult(i, JUDGE_ERROR, sres.time_ms, sres.memory_kb, None,
                              f"interactor {ires.status} {ires.note}: {imsg}")
        if p.checker == "testlib" and p.answer(i).exists():
            verdict, points, msg = self.check(p, i, tout)
            return TestResult(i, verdict, sres.time_ms, sres.memory_kb, points, msg)
        return TestResult(i, ACCEPTED, sres.time_ms, sres.memory_kb, None, imsg)

    def check(self, p: ProblemSpec, i: int, output: Path) -> tuple[str, float | None, str]:
        if p.checker != "testlib":
            ok, msg = token_compare(output, p.answer(i))
            return (ACCEPTED if ok else WRONG_ANSWER), None, msg
        log = self.io / "checker.txt"
        res = self.sb.run([str(p.dir / "check"), str(p.input(i)), str(output), str(p.answer(i))],
                          Limits(time_ms=CHECKER_TIME_MS, memory_mb=None), trusted=True,
                          stderr_path=log, cwd=self.io)
        msg = _read_capped(log, MESSAGE_CAP)
        code = res.exit_code
        if res.status in ("tle", "mle", "ole") or code is None:
            return JUDGE_ERROR, None, f"checker {res.status}: {msg}"
        if code == 0:
            return ACCEPTED, None, msg
        if code in (1, 2, 4):  # WA, PE (electicode has no PE), dirt
            return WRONG_ANSWER, None, msg
        if code == 7:  # quitp: partial points, message starts with the amount
            m = re.match(r"\s*points\s+([0-9.]+)|\s*([0-9.]+)", msg)
            pts = float(next(g for g in m.groups() if g)) if m else 0.0
            return (ACCEPTED if pts > 0 else WRONG_ANSWER), pts, msg
        return JUDGE_ERROR, None, f"checker exit code {code}: {msg}"

    # -- whole submission ---------------------------------------------------
    def judge(self, p: ProblemSpec, lang_id: str, source: str, mode: str) -> Outcome:
        """``mode`` is ``icpc`` (stop at the first failure) or ``ioi`` (score every group)."""
        lang = languages.get(lang_id)
        ok, log, run = self.compile(lang, source)
        max_score = max_points(p)
        if not ok:
            return Outcome(COMPILATION_ERROR, 0.0, max_score, compile_output=log)
        try:
            if mode == "icpc":
                out = self._judge_icpc(p, lang, run)
            else:
                out = self._judge_ioi(p, lang, run)
        finally:
            self.sb.clean()
        out.compile_output = log
        out.max_score = max_score
        ran = [t for t in out.tests if t.verdict != SKIPPED]
        out.time_ms = max((t.time_ms for t in ran), default=0)
        out.memory_kb = max((t.memory_kb for t in ran), default=0)
        return out

    def _judge_icpc(self, p, lang, run) -> Outcome:
        results = []
        for t in sorted(p.tests, key=lambda t: t["index"]):
            r = self.run_test(p, lang, run, t["index"])
            results.append(r)
            if r.verdict != ACCEPTED:
                return Outcome(r.verdict, 0.0, failed_test=r.test, tests=results)
        return Outcome(ACCEPTED, max_points(p), tests=results)

    def _judge_ioi(self, p, lang, run) -> Outcome:
        results: dict[int, TestResult] = {}

        def run_one(i: int) -> TestResult:
            if i not in results:
                results[i] = self.run_test(p, lang, run, i)
            return results[i]

        group_out, score = [], 0.0
        if p.groups:
            passed: dict[str, bool] = {}
            for g in p.groups:
                idx = sorted(t["index"] for t in p.tests if str(t.get("group")) == g["name"])
                blocked = [d for d in g.get("dependencies", []) if not passed.get(d, False)]
                got, all_ok = 0.0, True
                if blocked:
                    all_ok = False
                    for i in idx:
                        results.setdefault(i, TestResult(i, SKIPPED))
                elif g.get("policy") == "each-test":
                    for i in idx:
                        r = run_one(i)
                        if r.verdict == ACCEPTED:
                            got += _test_points(p, i, r)
                        else:
                            all_ok = False
                else:  # complete-group: all or nothing, stop at the first failure
                    for i in idx:
                        if run_one(i).verdict != ACCEPTED:
                            all_ok = False
                            break
                    for i in idx:
                        results.setdefault(i, TestResult(i, SKIPPED))
                    got = float(g.get("points", 0)) if all_ok else 0.0
                passed[g["name"]] = all_ok
                score += got
                group_out.append({"name": g["name"], "score": got, "max": group_max(p, g),
                                  "passed": all_ok, "blocked_by": blocked})
            # Tests outside every group still count for the verdict.
            for t in p.tests:
                if str(t.get("group", "")) not in passed:
                    run_one(t["index"])
        else:
            for t in sorted(p.tests, key=lambda t: t["index"]):
                r = run_one(t["index"])
                if r.verdict == ACCEPTED:
                    score += _test_points(p, t["index"], r)
            if not any(t.get("points") for t in p.tests):
                score = max_points(p) if all(r.verdict == ACCEPTED for r in results.values()) else 0.0

        tests = [results[i] for i in sorted(results)]
        failed = next((t for t in tests if t.verdict not in (ACCEPTED, SKIPPED)), None)
        verdict = failed.verdict if failed else ACCEPTED
        return Outcome(verdict, round(score, 4), failed_test=failed.test if failed else None,
                       tests=tests, groups=group_out)


# -- scoring helpers ----------------------------------------------------------

def _test_points(p: ProblemSpec, i: int, r: TestResult) -> float:
    if r.points is not None:
        return r.points
    t = next((t for t in p.tests if t["index"] == i), {})
    return float(t.get("points") or 0)


def group_max(p: ProblemSpec, g: dict) -> float:
    if g.get("policy") == "each-test":
        return float(sum(t.get("points") or 0 for t in p.tests if str(t.get("group")) == g["name"]))
    return float(g.get("points") or 0)


def max_points(p: ProblemSpec) -> float:
    if p.groups:
        total = sum(group_max(p, g) for g in p.groups)
    else:
        total = sum(float(t.get("points") or 0) for t in p.tests)
    return float(total) if total > 0 else 100.0


# -- output comparison ----------------------------------------------------------

def token_compare(output: Path, answer: Path) -> tuple[bool, str]:
    """Whitespace-insensitive token comparison (testlib's ``wcmp``)."""
    with open(output, "rb") as f:
        got = f.read().split()
    with open(answer, "rb") as f:
        want = f.read().split()
    for n, (a, b) in enumerate(zip(got, want), 1):
        if a != b:
            return False, f"token {n} differs: expected {_short(b)}, found {_short(a)}"
    if len(got) != len(want):
        what = "extra tokens in output" if len(got) > len(want) else "output ended early"
        return False, f"{what}: expected {len(want)} tokens, found {len(got)}"
    return True, f"{len(want)} tokens"


def _short(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return repr(s if len(s) <= 40 else s[:37] + "...")


def _from_run(i: int, res: RunResult) -> TestResult:
    return TestResult(i, _RUN_TO_VERDICT[res.status], res.time_ms, res.memory_kb, None, res.note)


def _read_capped(path: Path, cap: int) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        data = f.read(cap + 1)
    text = data[:cap].decode("utf-8", "replace")
    return text + ("\n…(truncated)" if len(data) > cap else "")
