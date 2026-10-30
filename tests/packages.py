"""Build small Polygon-shaped packages for the tests.

No testlib here: the checker and interactor are plain C++ that follow testlib's
calling convention and exit codes (0 OK, 1 WA, 3 FAIL), which is all the judge
relies on.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

AB_MAIN = r"""
#include <bits/stdc++.h>
int main(){ long long a,b; std::cin>>a>>b; std::cout<<a+b<<"\n"; }
"""

AB_GEN = r"""
#include <bits/stdc++.h>
int main(int argc, char** argv){ printf("%s %s\n", argv[1], argv[2]); }
"""

# Accepts any output whose number equals the answer's (exact integer compare).
AB_CHECK = r"""
#include <bits/stdc++.h>
int main(int argc, char** argv){
    std::ifstream out(argv[2]), ans(argv[3]);
    long long x, y;
    if(!(out >> x)) { fprintf(stderr, "no number in output"); return 1; }
    ans >> y;
    if(x != y) { fprintf(stderr, "expected %lld, found %lld", y, x); return 1; }
    fprintf(stderr, "ok %lld", x); return 0;
}
"""

GUESS_INTERACTOR = r"""
#include <bits/stdc++.h>
int main(int argc, char** argv){
    std::ifstream in(argv[1]); std::ofstream tout(argv[2]);
    long long secret; in >> secret;
    for(int q = 0; q < 40; q++){
        std::string op; long long x;
        if(!(std::cin >> op >> x)){ fprintf(stderr, "unexpected EOF"); return 1; }
        if(op == "!"){
            if(x == secret){ tout << "ok " << q << "\n"; fprintf(stderr, "guessed in %d", q); return 0; }
            fprintf(stderr, "wrong guess %lld", x); return 1;
        }
        std::cout << (secret < x ? "<" : secret > x ? ">" : "=") << std::endl;
    }
    fprintf(stderr, "too many queries"); return 1;
}
"""

GUESS_MAIN = r"""
#include <bits/stdc++.h>
int main(){
    long long lo = 1, hi = 1000000000;
    while(lo < hi){
        long long mid = (lo + hi) / 2;
        std::cout << "? " << mid << std::endl;
        std::string r; std::cin >> r;
        if(r == "=") { lo = hi = mid; break; }
        if(r == "<") hi = mid - 1; else lo = mid + 1;
    }
    std::cout << "! " << lo << std::endl;
}
"""

AB_XML = """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<problem revision="3" short-name="a-plus-b">
  <names>
    <name language="english" value="A + B"/>
    <name language="russian" value="A + B (ru)"/>
  </names>
  <statements>
    <statement charset="UTF-8" language="english" path="statements/english/problem.tex" type="application/x-tex"/>
  </statements>
  <judging cpu-name="Intel" cpu-speed="3600" input-file="" output-file="">
    <testset name="tests">
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <test-count>6</test-count>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true" group="0"/>
        <test method="manual" sample="true" group="0"/>
        <test method="manual" group="1"/>
        <test method="manual" group="1"/>
        <test method="manual" group="2" points="30"/>
        <test cmd="gen 4000000000 7" method="generated" group="2" points="30"/>
      </tests>
      <groups>
        <group feedback-policy="complete" name="0" points="0" points-policy="complete-group"/>
        <group feedback-policy="complete" name="1" points="40" points-policy="complete-group">
          <dependencies><dependency group="0"/></dependencies>
        </group>
        <group feedback-policy="complete" name="2" points="60" points-policy="each-test"/>
      </groups>
    </testset>
  </judging>
  <files>
    <executables>
      <executable><source path="files/gen.cpp" type="cpp.g++17"/></executable>
    </executables>
  </files>
  <assets>
    <checker name="check.cpp" type="testlib">
      <source path="files/check.cpp" type="cpp.g++17"/>
    </checker>
    <solutions>
      <solution tag="main"><source path="solutions/ab.cpp" type="cpp.g++17"/></solution>
      <solution tag="wrong-answer"><source path="solutions/wa.cpp" type="cpp.g++17"/></solution>
    </solutions>
  </assets>
</problem>
"""

GUESS_XML = """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<problem revision="1" short-name="guess">
  <names><name language="english" value="Guess the Number"/></names>
  <judging>
    <testset name="tests">
      <time-limit>2000</time-limit>
      <memory-limit>268435456</memory-limit>
      <test-count>3</test-count>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
        <test method="manual"/>
        <test method="manual"/>
      </tests>
    </testset>
  </judging>
  <assets>
    <interactor><source path="files/interactor.cpp" type="cpp.g++17"/></interactor>
    <solutions>
      <solution tag="main"><source path="solutions/main.cpp" type="cpp.g++17"/></solution>
    </solutions>
  </assets>
</problem>
"""


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def a_plus_b(path: Path) -> Path:
    props = {
        "name": "A + B",
        "legend": "Given two integers $a$ and $b$, print \\textbf{their sum}~--- that's all.\n\n"
                  "\\begin{itemize}\n\\item first\n\\item second $$a+b$$\n\\end{itemize}",
        "input": "One line with $a$ and $b$ ($1 \\le a, b \\le 10^9$).",
        "output": "Print $a + b$.",
        "notes": "",
        "sampleTests": [{"input": "1 2\n", "output": "3\n"}, {"input": "5 5\n", "output": "10\n"}],
    }
    files = {
        "problem.xml": AB_XML,
        "files/gen.cpp": AB_GEN,
        "files/check.cpp": AB_CHECK,
        "solutions/ab.cpp": AB_MAIN,
        "solutions/wa.cpp": "int main(){return 0;}",
        "statements/english/problem-properties.json": json.dumps(props),
        "tests/01": "1 2\n", "tests/01.a": "3\n",
        "tests/02": "5 5\n", "tests/02.a": "10\n",
        "tests/03": "100 200\n", "tests/03.a": "300\n",
        "tests/04": "999999999 1\n", "tests/04.a": "1000000000\n",
        "tests/05": "3000000000 3000000000\n",   # answer computed by the main solution
        # test 6: input generated by `gen`, answer computed
    }
    return _zip(path, files)


def guess(path: Path) -> Path:
    return _zip(path, {
        "problem.xml": GUESS_XML,
        "files/interactor.cpp": GUESS_INTERACTOR,
        "solutions/main.cpp": GUESS_MAIN,
        "tests/01": "42\n", "tests/02": "1\n", "tests/03": "999999999\n",
    })
