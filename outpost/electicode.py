"""Import problems backed up from electicode (the Outpost side of the bridge).

Platform-Scraper's ``course_backup.py --full-problems`` saves each problem as::

    problems/<slug>/judge.json      {metadata: {name, time_limit_ms, memory_limit_kb,
                                     verifier_type, test_points?, subtasks?}, testCases: [names]}
    problems/<slug>/record.json     the display record: displayName[Ru|Tg|Uz],
                                     displayDescription[Ru|Tg|Uz] (Markdown + $math$),
                                     sampleTestCases
    problems/<slug>/tests/<n>.in|.out
    problems/<slug>/source.json     {verifier, interactor} sources (custom / interactive only)

Outpost imports that folder (or a zip of it, or a whole backup's ``problems/``)
into the same problem layout a Polygon package produces. A custom checker or
interactor is compiled from source.json and called the testlib way; a problem
that needs one but has no source.json is refused rather than judged wrongly.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import db, polygon

LANGS = {"": "english", "Ru": "russian", "Tg": "tajik", "Uz": "uzbek"}


class ElecticodeError(polygon.ImportError_):
    pass


def looks_like(root: Path) -> bool:
    return (root / "judge.json").is_file() and (root / "tests").is_dir()


def find_problems(path: Path) -> list[Path]:
    """One problem folder, or every problem folder under a backup."""
    if looks_like(path):
        return [path]
    return sorted(p.parent for p in path.rglob("judge.json") if looks_like(p.parent))


def markdown_to_html(text: str | None) -> str:
    """Statement Markdown -> HTML, the $math$ handed to KaTeX untouched."""
    if not text or not text.strip():
        return ""
    import html

    import markdown
    maths: list[str] = []

    def stash(m: re.Match) -> str:
        display = m.group(1) is not None
        body = m.group(1) if display else m.group(2)
        maths.append((r"\[" if display else r"\(") + html.escape(body.strip(), quote=False)
                     + (r"\]" if display else r"\)"))
        return f"\x00{len(maths) - 1}\x00"

    text = re.sub(r"\$\$(.+?)\$\$|(?<![\\$])\$(?!\$)(.+?)(?<!\\)\$", stash, text, flags=re.S)
    out = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return re.sub(r"\x00(\d+)\x00", lambda m: maths[int(m.group(1))], out)


def _tests_and_groups(meta: dict, names: list[str]) -> tuple[list[dict], list[dict]]:
    points = meta.get("test_points") or []
    tests = [{"index": n, "group": "", "points": float(points[n - 1]) if n - 1 < len(points) else 0.0,
              "sample": False} for n in range(1, len(names) + 1)]
    groups = []
    for g in meta.get("subtasks") or []:
        name = str(g.get("name") or len(groups) + 1)
        for i in g.get("test_indices") or []:
            if 1 <= int(i) <= len(tests):
                tests[int(i) - 1]["group"] = name
        groups.append({"name": name, "points": float(g.get("points") or 0), "policy": "complete-group",
                       "dependencies": []})
    return tests, groups


def build(root: Path, dest: Path, log: polygon.Log) -> dict:
    """Lay one backed-up electicode problem out in ``dest``; return its DB fields."""
    judge = json.loads((root / "judge.json").read_text(encoding="utf-8"))
    meta, names = judge.get("metadata") or {}, [str(n) for n in judge.get("testCases") or []]
    record = json.loads((root / "record.json").read_text(encoding="utf-8")) if (root / "record.json").exists() else {}
    slug = root.name
    if not names:
        raise ElecticodeError(f"{slug}: judge.json lists no tests")
    tests_dir = dest / "tests"
    tests_dir.mkdir(parents=True)
    for n, name in enumerate(names, 1):
        src_in, src_out = root / "tests" / f"{name}.in", root / "tests" / f"{name}.out"
        if not src_in.exists() or not src_out.exists():
            raise ElecticodeError(f"{slug}: test {name} is missing from the backup (run it again without --no-tests)")
        shutil.copy2(src_in, tests_dir / f"{n:02d}")
        shutil.copy2(src_out, tests_dir / f"{n:02d}.a")
    log(f"{slug}: {len(names)} tests")

    kind = meta.get("verifier_type") or "exact_match"
    sources = json.loads((root / "source.json").read_text(encoding="utf-8")) if (root / "source.json").exists() else {}
    checker, interactive = "builtin", False
    files = dest / "src"
    for role, target in (("verifier", "check"), ("interactor", "interactor")):
        needed = (role == "verifier" and kind not in ("exact_match", "interactive")) or \
                 (role == "interactor" and kind == "interactive")
        code = sources.get(role)
        if needed and not code:
            raise ElecticodeError(
                f"{slug}: judged with a {kind} {role} on electicode, but the backup has no source.json. "
                f"Back it up again with a Platform-Scraper that saves sources.")
        if code:
            files.mkdir(exist_ok=True)
            (files / f"{target}.cpp").write_text(code, encoding="utf-8")
    if (files / "check.cpp").exists() or (files / "interactor.cpp").exists():
        testlib = Path(__file__).with_name("testlib.h")
        if testlib.exists():  # electicode's checkers are testlib ones; the header may not be in the backup
            shutil.copy2(testlib, files / "testlib.h")
        polygon.rebuild_binaries(dest, log)
        checker = "testlib" if (dest / "check").exists() else "builtin"
        interactive = (dest / "interactor").exists()

    statements = {}
    samples = record.get("sampleTestCases") if isinstance(record.get("sampleTestCases"), list) else []
    sample_inputs = []
    for s in samples:  # a sample is one of the tests, by its order
        i = int(s.get("orderIndex") or 0)
        if 1 <= i <= len(names):
            sample_inputs.append(i)
    for suffix, lang in LANGS.items():
        name = record.get(f"displayName{suffix}") or (meta.get("name") if not suffix else None)
        text = record.get(f"displayDescription{suffix}") or (meta.get("description") if not suffix else None)
        if not name and not text:
            continue
        statements[lang] = {
            "name": name or slug, "legend": markdown_to_html(text), "input": "", "output": "", "interaction": "",
            "notes": "", "scoring": "", "tutorial": "", "pdf": False,
            "samples": [{"input": (tests_dir / f"{i:02d}").read_text(errors="replace"),
                         "output": (tests_dir / f"{i:02d}.a").read_text(errors="replace")} for i in sample_inputs],
        }
    tests, groups = _tests_and_groups(meta, names)
    for i in sample_inputs:
        tests[i - 1]["sample"] = True
    title = record.get("displayName") or meta.get("name") or slug
    return {
        "slug": slug, "name": title, "time_limit_ms": int(meta.get("time_limit_ms") or 1000),
        "memory_limit_mb": max(1, int(meta.get("memory_limit_kb") or 262144) // 1024),
        "interactive": int(interactive), "checker": checker, "tests_json": db.dumps(tests),
        "groups_json": db.dumps(groups), "statements_json": db.dumps(statements), "main_max_ms": None,
    }
