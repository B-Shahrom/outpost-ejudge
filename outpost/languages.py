"""Submission languages: how to compile and how to run.

The ids match electicode's (``cpp``, ``java21``, ``py12``) so submissions and
filters line up with the platform.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Language:
    id: str
    name: str
    source_name: str                     # file the source is saved as in the box
    compile: list[str] | None            # argv, or None for interpreted languages
    run: list[str]                       # argv; {mem} expands to the memory limit in MB
    address_space: bool = True           # cap virtual memory (the JVM can't take it)
    rss_slack_mb: int = 0
    processes: int = 64
    time_factor: float = 1.0             # TL multiplier for this language
    extensions: tuple[str, ...] = field(default=())

    def run_argv(self, memory_mb: int) -> list[str]:
        return [a.replace("{mem}", str(memory_mb)) for a in self.run]

    def available(self) -> bool:
        tool = (self.compile or self.run)[0]
        return shutil.which(tool) is not None


def _python() -> str:
    for name in ("python3.12", "python3"):
        if shutil.which(name):
            return name
    return "python3"


LANGUAGES: dict[str, Language] = {
    lang.id: lang for lang in [
        Language(
            id="cpp", name="GNU C++20", source_name="main.cpp",
            compile=["g++", "-std=gnu++20", "-O2", "-pipe", "-DONLINE_JUDGE", "-o", "main", "main.cpp"],
            run=["./main"], extensions=(".cpp", ".cc", ".cxx", ".c++"),
        ),
        Language(
            id="java21", name="Java", source_name="Main.java",
            compile=["javac", "-encoding", "UTF-8", "-J-Xmx512m", "Main.java"],
            run=["java", "-Xmx{mem}m", "-Xss64m", "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "Main"],
            address_space=False, rss_slack_mb=96, processes=256, time_factor=2.0,
            extensions=(".java",),
        ),
        Language(
            id="py12", name="Python 3", source_name="main.py",
            compile=None, run=[_python(), "main.py"], time_factor=1.0,
            extensions=(".py",),
        ),
    ]
}


def prepare_pch(pch_dir: Path, log=print) -> Path | None:
    """Precompile ``<bits/stdc++.h>`` for the C++ flags, once per g++ version.

    Nearly every submission starts with that header, and parsing it is most of
    a compile (about 2.5 s of 3 on a laptop, 0.7 s with the precompiled one).
    g++ picks ``<dir>/bits/stdc++.h.gch`` up automatically when ``-I <dir>``
    comes first and the flags match; if anything doesn't match it silently
    parses the header as usual, so this can only make compiles faster.
    """
    cpp = LANGUAGES["cpp"]
    if not shutil.which("g++"):
        return None
    flags = [a for a in cpp.compile[1:] if a not in ("-o", "main", "main.cpp")]
    version = subprocess.run(["g++", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
    stamp_text = version + "\n" + " ".join(flags)
    gch = pch_dir / "bits" / "stdc++.h.gch"
    stamp = pch_dir / "stamp"

    def ready() -> bool:
        return gch.exists() and stamp.exists() and stamp.read_text() == stamp_text

    if ready():
        return pch_dir
    gch.parent.mkdir(parents=True, exist_ok=True)
    # The server and `outpost selftest` may both get here at once (the start
    # scripts run them back to back): one builds, the other waits and reuses it.
    with open(pch_dir / "build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready():
            return pch_dir
        log("Preparing the C++ precompiled header (once per g++ version, ~10 s)…")
        wrapper = pch_dir / "pch.h"
        wrapper.write_text("#include <bits/stdc++.h>\n")
        tmp = gch.with_name(f"stdc++.h.gch.{os.getpid()}.tmp")
        res = subprocess.run(["g++", *flags, "-x", "c++-header", str(wrapper), "-o", str(tmp)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            tmp.unlink(missing_ok=True)
            log("  could not build it (compiles will just be slower): " + res.stderr[-300:])
            return None
        os.replace(tmp, gch)
        stamp.write_text(stamp_text)
    # Sandboxed compiles run as other uids and must be able to read it.
    for d in (pch_dir, gch.parent):
        d.chmod(0o755)
    gch.chmod(0o644)
    return pch_dir


def get(lang_id: str) -> Language:
    return LANGUAGES[lang_id]


def guess(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    for lang in LANGUAGES.values():
        if ext in lang.extensions:
            return lang.id
    return None


def from_polygon_type(kind: str) -> str | None:
    """Map a Polygon source type (``cpp.g++17``, ``java21``, ``python.3``) to ours."""
    kind = kind.lower()
    if kind.startswith(("cpp", "c.", "c++")):
        return "cpp"
    if kind.startswith("java"):
        return "java21"
    if kind.startswith("python") and "2" not in kind.split(".", 1)[-1][:1]:
        return "py12"
    return None
