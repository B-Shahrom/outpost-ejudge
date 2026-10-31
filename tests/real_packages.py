"""Packages laid out the way Polygon builds them, using the real testlib.

``tests/fixtures/testlib`` holds an unmodified testlib.h, its standard ``wcmp``
checker and its A+B interactor (MIT, see the LICENSE beside them). The
problem.xml files carry everything a real one does — binaries for Windows,
checker and validator testsets, tags, properties, a ``doall`` script — so the
importer is tested against the noise it will meet, not a minimal subset.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "testlib"
TESTLIB = (FIX / "testlib.h").read_text()
WCMP = (FIX / "wcmp.cpp").read_text()
AB_INTERACTOR = (FIX / "interactor-a-plus-b.cpp").read_text()

GEN = r"""
#include "testlib.h"
#include <bits/stdc++.h>
using namespace std;
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int n = opt<int>("n");
    long long mx = opt<long long>("max");
    println(n);
    vector<long long> a(n);
    for (auto& x : a) x = rnd.next(1LL, mx);
    println(a);
}
"""

VALIDATOR = r"""
#include "testlib.h"
int main(int argc, char* argv[]) {
    registerValidation(argc, argv);
    int n = inf.readInt(1, 100000, "n"); inf.readEoln();
    for (int i = 0; i < n; i++) { inf.readLong(1LL, 1000000000LL, "a_i"); if (i + 1 < n) inf.readSpace(); }
    inf.readEoln(); inf.readEof();
}
"""

SUM_MAIN = r"""
#include <bits/stdc++.h>
using namespace std;
int main() { int n; cin >> n; long long s = 0; for (int i = 0; i < n; i++) { long long x; cin >> x; s += x; } cout << s << "\n"; }
"""
SUM_WA = r"""
#include <bits/stdc++.h>
using namespace std;
int main() { int n; cin >> n; int s = 0; for (int i = 0; i < n; i++) { int x; cin >> x; s += x; } cout << s << "\n"; }
"""

# Scored checker: full points for the exact sum, half for being off by < 10.
POINTS_CHECK = r"""
#include "testlib.h"
int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);
    long long ja = ans.readLong(), pa = ouf.readLong();
    if (ja == pa) quitf(_ok, "exact");
    if (llabs(ja - pa) < 10) quitp(5.0, "close: expected %lld, found %lld", ja, pa);
    quitf(_wa, "expected %lld, found %lld", ja, pa);
}
"""

AB_SOLUTION = r"""
#include <bits/stdc++.h>
int main() { long long a, b; while (std::cin >> a >> b) std::cout << a + b << std::endl; }
"""


def _xml(short_name: str, *, tests: list[dict], groups: str, checker: str, interactor: bool = False,
         solutions: dict[str, str], tl: int = 1000) -> str:
    test_xml = "\n".join(
        "        <test" + "".join(f' {k}="{v}"' for k, v in t.items()) + "/>" for t in tests)
    inter = """
    <interactor>
      <source path="files/interactor.cpp" type="cpp.g++17"/>
      <binary path="files/interactor.exe" type="exe.win32"/>
    </interactor>""" if interactor else ""
    sols = "\n".join(f'      <solution tag="{tag}"><source path="solutions/{f}" type="cpp.g++17"/>'
                     f'<binary path="solutions/{f[:-4]}.exe" type="exe.win32"/></solution>'
                     for f, tag in solutions.items())
    return f"""<?xml version="1.0" encoding="utf-8" standalone="no"?>
<problem revision="42" short-name="{short_name}" url="https://polygon.codeforces.com/p1234/someone/{short_name}">
  <names>
    <name language="english" value="Sum of the Array"/>
    <name language="russian" value="Сумма массива"/>
  </names>
  <statements>
    <statement charset="UTF-8" language="english" mathjax="true" path="statements/english/problem.tex" type="application/x-tex"/>
    <statement charset="UTF-8" language="english" mathjax="true" path="statements/.html/english/problem.html" type="text/html"/>
    <statement language="english" path="statements/.pdf/english/problem.pdf" type="application/pdf"/>
    <statement charset="UTF-8" language="russian" mathjax="true" path="statements/russian/problem.tex" type="application/x-tex"/>
  </statements>
  <tutorials>
    <tutorial charset="UTF-8" language="english" mathjax="true" path="statements/english/tutorial.tex" type="application/x-tex"/>
  </tutorials>
  <judging cpu-name="Intel(R) Core(TM) i3-8100 CPU @ 3.60GHz" cpu-speed="3600" input-file="" output-file="" run-count="1">
    <testset name="tests">
      <time-limit>{tl}</time-limit>
      <memory-limit>268435456</memory-limit>
      <test-count>{len(tests)}</test-count>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
{test_xml}
      </tests>
{groups}
    </testset>
  </judging>
  <files>
    <resources>
      <file path="files/olymp.sty"/>
      <file path="files/problem.tex"/>
      <file path="files/statements.ftl"/>
      <file path="files/testlib.h" type="h.g++"/>
    </resources>
    <executables>
      <executable><source path="files/gen.cpp" type="cpp.g++17"/><binary path="files/gen.exe" type="exe.win32"/></executable>
      <executable><source path="files/val.cpp" type="cpp.g++17"/><binary path="files/val.exe" type="exe.win32"/></executable>
    </executables>
  </files>
  <assets>
    <checker name="{checker}" type="testlib">
      <source path="files/check.cpp" type="cpp.g++17"/>
      <binary path="check.exe" type="exe.win32"/>
      <copy path="check.cpp"/>
      <testset>
        <test-count>0</test-count>
        <input-path-pattern>files/tests/checker-tests/%02d</input-path-pattern>
        <output-path-pattern>files/tests/checker-tests/%02d.o</output-path-pattern>
        <answer-path-pattern>files/tests/checker-tests/%02d.a</answer-path-pattern>
        <tests/>
      </testset>
    </checker>{inter}
    <validators>
      <validator>
        <source path="files/val.cpp" type="cpp.g++17"/>
        <binary path="files/val.exe" type="exe.win32"/>
        <testset><test-count>0</test-count><input-path-pattern>files/tests/validator-tests/%02d</input-path-pattern><tests/></testset>
      </validator>
    </validators>
    <solutions>
{sols}
    </solutions>
  </assets>
  <properties>
    <property name="tests-wellformed" value="true"/>
  </properties>
  <stresses><stress-count>0</stress-count><stress-path-pattern>stresses/%03d</stress-path-pattern><list/></stresses>
  <tags>
    <tag value="implementation"/>
  </tags>
</problem>
"""


def _statement(lang: str, name: str, legend: str) -> dict[str, str]:
    props = {
        "name": name, "legend": legend,
        "input": "The first line contains $$$n$$$ ($$$1 \\le n \\le 10^5$$$). "
                 "The second line contains $$$a_1, \\ldots, a_n$$$ ($$$1 \\le a_i \\le 10^9$$$).",
        "output": "Print $$$\\sum a_i$$$.",
        "interaction": None, "scoring": None, "notes": "", "tutorial": "",
        "inputFile": "stdin", "outputFile": "stdout", "authorLogin": "someone", "authorName": "",
        "language": lang, "timeLimit": 1000, "memoryLimit": 268435456,
        "sampleTests": [{"input": "3\n1 2 3\n", "output": "6\n", "inputFile": "example.01",
                         "outputFile": "example.01.a"}],
        "extraResources": {},
    }
    base = f"statements/{lang}/"
    return {
        base + "problem-properties.json": json.dumps(props, ensure_ascii=False),
        base + "legend.tex": legend, base + "input.tex": props["input"], base + "output.tex": props["output"],
        base + "name.tex": name, base + "example.01": "3\n1 2 3\n", base + "example.01.a": "6\n",
        base + "problem.tex": "\\begin{problem}...\\end{problem}",
    }


def tiny_pdf(pages: int = 1) -> bytes:
    """A real, valid PDF of blank pages, as small as pypdf writes one."""
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _common(xml: str, pdf: bytes | None = None) -> dict[str, str | bytes]:
    files: dict[str, str | bytes] = {
        "problem.xml": xml,
        "files/testlib.h": TESTLIB,
        "files/olymp.sty": "% olymp.sty", "files/problem.tex": "", "files/statements.ftl": "",
        "files/gen.cpp": GEN, "files/val.cpp": VALIDATOR,
        "doall.sh": "#!/bin/bash\n# Polygon's own build script; Outpost must not need it\nexit 1\n",
        "doall.bat": "exit 1\r\n",
        "statements/.html/english/problem.html": "<html><body>generated</body></html>",
        "statements/.html/english/problem-statement.css": "",
        "statements/.pdf/english/problem.pdf": tiny_pdf() if pdf is None else pdf,
        "statements/english/tutorial.tex": "Just add them up in a long long.",
    }
    files.update(_statement("english", "Sum of the Array",
                            "Find the sum of an array. Mind the \\textbf{overflow}."))
    files.update(_statement("russian", "Сумма массива", "Найдите сумму массива."))
    return files


def standard_package(path: Path) -> Path:
    """Like Polygon's *Standard* package: generated tests and all answers missing."""
    tests = [
        {"method": "manual", "sample": "true", "group": "0", "points": "0.0"},
        {"method": "manual", "group": "1", "points": "0.0"},
        {"cmd": "gen -n 5 -max 100 1", "method": "generated", "group": "1", "points": "0.0"},
        {"cmd": "gen -n 1000 -max 1000000000 2", "method": "generated", "group": "2", "points": "0.0"},
        {"cmd": "gen -n 100000 -max 1000000000 3", "method": "generated", "group": "2", "points": "0.0"},
    ]
    groups = """      <groups>
        <group feedback-policy="complete" name="0" points="0.0" points-policy="complete-group"/>
        <group feedback-policy="complete" name="1" points="30.0" points-policy="complete-group">
          <dependencies><dependency group="0"/></dependencies>
        </group>
        <group feedback-policy="icpc" name="2" points="70.0" points-policy="complete-group">
          <dependencies><dependency group="1"/></dependencies>
        </group>
      </groups>"""
    files = _common(_xml("array-sum", tests=tests, groups=groups, checker="std::wcmp.cpp",
                         solutions={"sol.cpp": "main", "wa.cpp": "wrong-answer"}))
    files.update({
        "files/check.cpp": WCMP, "check.cpp": WCMP,
        "solutions/sol.cpp": SUM_MAIN, "solutions/wa.cpp": SUM_WA,
        "tests/01": "3\n1 2 3\n", "tests/02": "2\n5 7\n",
    })
    return _zip(path, files)


def points_package(path: Path, short_name: str = "close-sum", pdf: bytes | None = None) -> Path:
    """A *Linux* package (tests and answers included) whose checker gives partial points.

    ``pdf`` replaces its English statement PDF, a valid one-page PDF by default."""
    tests = [{"method": "manual", "sample": "true", "points": "0.0"},
             {"method": "manual", "points": "10.0"}, {"method": "manual", "points": "10.0"}]
    files = _common(_xml(short_name, tests=tests, groups="", checker="check.cpp",
                         solutions={"sol.cpp": "main"}), pdf)
    files.update({
        "files/check.cpp": POINTS_CHECK, "check.cpp": POINTS_CHECK, "solutions/sol.cpp": SUM_MAIN,
        "tests/01": "3\n1 2 3\n", "tests/01.a": "6\n",
        "tests/02": "2\n100 200\n", "tests/02.a": "300\n",
        "tests/03": "1\n7\n", "tests/03.a": "7\n",
    })
    return _zip(path, files)


def interactive_package(path: Path) -> Path:
    """testlib's own A+B interactor, checked by wcmp against the interactor's tout."""
    tests = [{"method": "manual", "sample": "true"}, {"method": "manual"}]
    files = _common(_xml("ab-interactive", tests=tests, groups="", checker="std::wcmp.cpp",
                         interactor=True, solutions={"sol.cpp": "main"}))
    files.update({
        "files/check.cpp": WCMP, "check.cpp": WCMP, "files/interactor.cpp": AB_INTERACTOR,
        "solutions/sol.cpp": AB_SOLUTION,
        "tests/01": "2\n1 2\n3 4\n", "tests/02": "3\n1000 2000\n-5 5\n0 0\n",
    })
    return _zip(path, files)


def _zip(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            # Polygon zips the problem inside a folder named after it.
            zf.writestr(f"{path.stem}/{name}", content)
    return path
