"""``outpost selftest``: is this machine ready to judge a contest?

Judges a handful of known programs on a throwaway A+B problem and checks each
verdict, then checks the sandbox: no network, no reading the judge's files.
Run it on contest morning (the start scripts run it every time).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import config, languages
from .judge import ProblemSpec
from .sandbox import network_isolation_available
from .worker import make_judge

CPP_AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
CASES = [
    ("C++ correct", "cpp", CPP_AB, "accepted"),
    ("C++ wrong", "cpp", "#include <bits/stdc++.h>\nint main(){std::cout<<0;}", "wrong_answer"),
    ("C++ endless loop", "cpp", "int main(){volatile long x=0;for(;;)x++;}", "time_limit_exceeded"),
    ("C++ huge array", "cpp", "#include <bits/stdc++.h>\nint main(){std::vector<long long>v(1e9,1);"
                              "std::cout<<v[5];}", "memory_limit_exceeded"),
    ("C++ crash", "cpp", "int main(){int*p=0;return *p;}", "runtime_error"),
    ("C++ syntax error", "cpp", "int main( {", "compilation_error"),
    ("Python correct", "py12", "a, b = map(int, input().split())\nprint(a + b)", "accepted"),
    ("Java correct", "java21", "import java.util.*;public class Main{public static void main(String[] x)"
                               "{Scanner s=new Scanner(System.in);System.out.println(s.nextLong()+s.nextLong());}}",
     "accepted"),
]
NETWORK_PROBE = ("import socket,sys\ns=socket.socket();s.settimeout(3)\n"
                 "try:\n s.connect(('1.1.1.1',53))\n print('CONNECTED')\nexcept OSError:\n print('blocked')")


def run(out=print) -> int:
    real = config.load()
    tmp = Path(tempfile.mkdtemp(prefix="outpost-selftest-"))
    tmp.chmod(0o711)
    # Same uids and precompiled header as the real judge, throwaway everything else.
    settings = dataclasses.replace(real, data_dir=tmp)
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        out(f"  {'ok  ' if ok else 'FAIL'}  {name}{('  -  ' + detail) if detail else ''}")

    try:
        settings.ensure_dirs()
        pdir = settings.problems_dir / "selftest"
        (pdir / "tests").mkdir(parents=True)
        (pdir / "tests" / "01").write_text("1 2\n")
        (pdir / "tests" / "01.a").write_text("3\n")
        spec = ProblemSpec(dir=pdir, time_limit_ms=1000, memory_limit_mb=256,
                           tests=[{"index": 1, "group": "", "points": 0, "sample": True}])
        isolated = network_isolation_available()
        pch = languages.prepare_pch(real.pch_dir, log=lambda line: out("  " + line))
        judge = make_judge(settings, 98, isolated)
        judge.pch_dir = pch

        out("Judging known programs:")
        for name, lang, src, want in CASES:
            if not languages.get(lang).available():
                report(name, lang != "cpp", f"{languages.get(lang).name} is not installed")
                continue
            t = time.monotonic()
            got = judge.judge(spec, lang, src, "icpc").verdict
            report(name, got == want, f"{got} in {time.monotonic() - t:.1f} s" if got == want
                   else f"expected {want}, got {got}")

        out("Sandbox:")
        judge.sb.prepare()
        probe = judge.sb.box / "probe.py"
        probe.write_text(NETWORK_PROBE)
        judge.sb.give(probe)
        net_out = judge.io / "net.txt"
        judge.io.mkdir(parents=True, exist_ok=True)
        with open(net_out, "wb") as f:
            judge.sb.run(["python3", "probe.py"], _limits(), stdout=f)
        text = net_out.read_text().strip()
        report("no network for submissions", text == "blocked",
               "isolated" if text == "blocked" else
               (f"probe said {text!r}" + ("" if isolated else
                " (no network namespaces: run as root, or the container needs CAP_SYS_ADMIN)")))
        if os.geteuid() == 0:
            secret = settings.problems_dir / "selftest" / "tests" / "01.a"
            reader = judge.sb.run(["cat", str(secret)], _limits(), stdout=open(os.devnull, "wb"))
            report("submissions can't read answers", reader.exit_code != 0,
                   "denied" if reader.exit_code != 0 else "a submission could read the answer files")
        else:
            report("submissions can't read answers", False,
                   "not running as root, so runs aren't moved to their own user")
        judge.sb.clean()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out("Ready." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


def _limits():
    from .sandbox import Limits
    return Limits(time_ms=5000, memory_mb=None)
