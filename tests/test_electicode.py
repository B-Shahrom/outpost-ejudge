"""Electicode problems as Platform-Scraper's course_backup.py --full-problems saves them."""
from __future__ import annotations

import json
import shutil
import zipfile

import pytest

import real_packages as rp
from outpost import db, polygon, worker
from outpost.electicode import ElecticodeError, markdown_to_html

AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
AB_INT = "#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}"


def problem(root, slug, *, kind="exact_match", tests, record=None, meta=None, source=None):
    d = root / "problems" / slug
    (d / "tests").mkdir(parents=True)
    names = [str(n) for n in range(1, len(tests) + 1)]
    for name, (i, o) in zip(names, tests):
        (d / "tests" / f"{name}.in").write_text(i)
        (d / "tests" / f"{name}.out").write_text(o)
    m = {"name": slug, "description": "", "time_limit_ms": 1000, "memory_limit_kb": 262144,
         "test_count": len(tests), "verifier_type": kind, **(meta or {})}
    (d / "judge.json").write_text(json.dumps({"metadata": m, "testCases": names}))
    if record is not None:
        (d / "record.json").write_text(json.dumps(record, ensure_ascii=False))
    if source is not None:
        (d / "source.json").write_text(json.dumps(source))
    return d


@pytest.fixture
def backup(tmp_path):
    big = "3000000000 3000000000\n", "6000000000\n"
    problem(tmp_path, "sum-two", tests=[("1 2\n", "3\n"), ("5 5\n", "10\n"), ("2 2\n", "4\n"), big],
            meta={"subtasks": [{"name": "1", "points": 40, "test_indices": [1, 2, 3]},
                               {"name": "2", "points": 60, "test_indices": [4]}]},
            record={"displayName": "Sum of Two", "displayNameRu": "Сумма двух",
                    "displayDescription": "Print $a + b$ for **two** numbers.\n\n| a | b |\n|---|---|\n| 1 | 2 |",
                    "displayDescriptionRu": "Выведите $a + b$.",
                    "sampleTestCases": [{"orderIndex": 1, "explanation": "1 + 2 = 3"}]})
    problem(tmp_path, "sum-checked", kind="custom", tests=[("1 2\n", "3\n"), ("5 5\n", "10\n")],
            record={"displayName": "Checked sum"}, source={"verifier": rp.WCMP, "interactor": None})
    return tmp_path


def test_markdown_statement_keeps_math():
    out = markdown_to_html("Print $a_1 + b$ and **bold**.\n\n$$x^2$$")
    assert r"\(a_1 + b\)" in out and "<strong>bold</strong>" in out and r"\[x^2\]" in out


def test_import_a_whole_backup(settings, backup, conn):
    rec = polygon.Recorder()
    ids = polygon.import_path(backup, settings, rec)
    assert len(ids) == 2
    row = db.one(conn, "SELECT * FROM problems WHERE slug='sum-two'")
    st = db.loads(row["statements_json"])
    assert st["english"]["name"] == "Sum of Two" and st["russian"]["name"] == "Сумма двух"
    assert r"\(a + b\)" in st["english"]["legend"] and "<table>" in st["english"]["legend"]
    assert st["english"]["samples"] == [{"input": "1 2\n", "output": "3\n"}]
    assert [g["points"] for g in db.loads(row["groups_json"])] == [40.0, 60.0]
    assert db.one(conn, "SELECT checker FROM problems WHERE slug='sum-checked'")["checker"] == "testlib"
    # Judged like any problem: the int overflow loses subtask 2 only.
    spec = worker.spec_for(row, settings)
    j = worker.make_judge(settings, 17, False, False)
    assert j.judge(spec, "cpp", AB_INT, "ioi").score == 40
    assert j.judge(spec, "cpp", AB, "ioi").score == 100
    checked = worker.spec_for(db.one(conn, "SELECT * FROM problems WHERE slug='sum-checked'"), settings)
    assert j.judge(checked, "cpp", AB, "icpc").verdict == "accepted"
    assert j.judge(checked, "cpp", AB.replace("a+b", "a-b"), "icpc").verdict == "wrong_answer"


def test_custom_checker_without_source_is_refused(settings, tmp_path):
    problem(tmp_path, "no-source", kind="custom", tests=[("1\n", "1\n")], record={"displayName": "X"})
    with pytest.raises(ElecticodeError, match="no source.json"):
        polygon.import_path(tmp_path, settings, polygon.Recorder())


def test_zip_of_a_backup(settings, backup, tmp_path):
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in (backup / "problems").rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(backup))
    assert len(polygon.import_path(z, settings, polygon.Recorder())) == 2
    shutil.rmtree(backup)
