"""Contest archives: export from one Outpost, import into a fresh one."""
from __future__ import annotations

import dataclasses
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

import real_packages as rp
from outpost import archive, auth, db, polygon, scoring, worker

AB = "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
AB_INT = "#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}"


@pytest.fixture(scope="module")
def exported(settings, problems, tmp_path_factory):
    c = db.connect(settings.db_path)
    cid = c.execute("INSERT INTO contests (code, name, start_at, end_at, scoring, divisions_json, visible, "
                    "created_at) VALUES ('arch', 'Archived', 0, 4e9, 'ioi', '[\"Arch\"]', 0, 0)").lastrowid
    c.execute("INSERT INTO contest_problems VALUES (?, ?, 'A', 0, '[]')", (cid, problems["a-plus-b"]))
    c.execute("INSERT INTO contest_problems VALUES (?, ?, 'B', 1, '[]')", (cid, problems["guess"]))
    uids = [auth.create_user(c, f"arch{i}", f"pw{i}", display_name=f"Arch {i}", division="Arch")
            for i in range(2)]
    for uid, src in zip(uids, (AB, AB_INT)):
        c.execute("INSERT INTO submissions (contest_id, problem_id, user_id, language, source, submitted_at) "
                  "VALUES (?,?,?,?,?,?)", (cid, problems["a-plus-b"], uid, "cpp", src, 100.0))
    c.execute("INSERT INTO clarifications (contest_id, user_id, question, answer, public, created_at, "
              "answered_at) VALUES (?, ?, 'Q?', 'A.', 1, 1, 2)", (cid, uids[0]))
    worker.drain(settings)
    out = tmp_path_factory.mktemp("arch") / "arch.zip"
    counts = archive.export_contest(c, settings, "arch", out)
    board = scoring.standings(c, db.one(c, "SELECT * FROM contests WHERE id=?", (cid,)), live=True)
    return out, counts, [(r.username, r.score) for r in board["rows"]]


@pytest.fixture
def fresh(settings):
    data = Path(tempfile.mkdtemp(prefix="outpost-fresh-"))
    data.chmod(0o711)
    s = dataclasses.replace(settings, data_dir=data)
    s.ensure_dirs()
    db.init(s.db_path)
    return s


def test_archive_contents(exported):
    out, counts, _ = exported
    assert counts == {"problems": 2, "users": 2, "submissions": 2}
    names = zipfile.ZipFile(out).namelist()
    assert "archive.json" in names and "problems/a-plus-b/tests/01" in names
    assert "problems/a-plus-b/src/check.cpp" in names       # sources travel...
    assert "problems/a-plus-b/check" not in names           # ...binaries are rebuilt


def test_round_trip_into_a_fresh_outpost(exported, fresh):
    out, _, board_before = exported
    c = db.connect(fresh.db_path)
    lines: list[str] = []
    assert archive.import_contest(c, fresh, out, log=lines.append) == "arch"
    assert (fresh.problems_dir / "a-plus-b" / "check").exists()  # rebuilt from src/
    contest = db.one(c, "SELECT * FROM contests WHERE code='arch'")
    board = scoring.standings(c, contest, live=True)
    assert [(r.username, r.score) for r in board["rows"]] == board_before
    user = db.one(c, "SELECT * FROM users WHERE username='arch0'")
    assert auth.check_password("pw0", user["password_hash"])
    assert db.one(c, "SELECT answer FROM clarifications")["answer"] == "A."
    # The rebuilt checker really judges here.
    c.execute("INSERT INTO submissions (contest_id, problem_id, user_id, language, source, submitted_at) "
              "VALUES (?, (SELECT id FROM problems WHERE slug='a-plus-b'), ?, 'cpp', ?, 200)",
              (contest["id"], user["id"], AB))
    worker.drain(fresh)
    assert db.one(c, "SELECT verdict FROM submissions ORDER BY id DESC LIMIT 1")["verdict"] == "accepted"

    with pytest.raises(archive.ArchiveError, match="exists"):
        archive.import_contest(c, fresh, out)
    lines.clear()
    assert archive.import_contest(c, fresh, out, new_code="arch-2", log=lines.append) == "arch-2"
    assert "problem a-plus-b: already here, kept as it is" in lines
    assert db.one(c, "SELECT COUNT(*) AS n FROM users WHERE username LIKE 'arch%'")["n"] == 2


def test_problems_only_export(settings, exported, tmp_path):
    out = tmp_path / "set.zip"
    c = db.connect(settings.db_path)
    assert archive.export_contest(c, settings, "arch", out, include_people=False)["users"] == 0
    data = json.loads(zipfile.ZipFile(out).read("archive.json"))
    assert data["users"] == [] and data["submissions"] == [] and len(data["problems"]) == 2


def test_a_broken_archive_leaves_nothing_behind(exported, fresh, tmp_path):
    out, _, _ = exported
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(broken, "w") as dst:
        data = json.loads(src.read("archive.json"))
        data["submissions"][0]["slug"] = "no-such-problem"  # fails after problems were copied
        dst.writestr("archive.json", json.dumps(data))
        for name in src.namelist():
            if name != "archive.json":
                dst.writestr(name, src.read(name))
    c = db.connect(fresh.db_path)
    with pytest.raises(KeyError):
        archive.import_contest(c, fresh, broken, log=lambda _: None)
    assert db.one(c, "SELECT COUNT(*) AS n FROM contests")["n"] == 0
    assert db.one(c, "SELECT COUNT(*) AS n FROM problems")["n"] == 0
    assert not (fresh.problems_dir / "a-plus-b").exists()
    with pytest.raises(archive.ArchiveError, match="not an Outpost"):
        archive.import_contest(c, fresh, tmp_path / "missing.zip")


def test_archived_pdfs_that_dont_open_are_dropped(tmp_path):
    """Archives from Outposts that didn't check PDFs can carry a broken one."""
    from pypdf import PdfWriter
    problem = tmp_path / "sum"
    files = {"english": b"%PDF-1.4 fake\n%%EOF\n", "russian": rp.tiny_pdf(), "empty": None}
    for lang, content in files.items():
        (problem / "statements" / lang).mkdir(parents=True)
        pdf = problem / "statements" / lang / "problem.pdf"
        if content is None:
            PdfWriter().write(str(pdf))  # parses, but has no pages
        else:
            pdf.write_bytes(content)
    rec = polygon.Recorder()
    out = db.loads(archive._drop_broken_pdfs(problem, db.dumps({l: {"pdf": True} for l in files}), rec))
    assert out == {"english": {"pdf": False}, "russian": {"pdf": True}, "empty": {"pdf": False}}
    assert rec.lines[0].startswith("WARNING statement PDF sum/statements/english/problem.pdf dropped: "
                                   "it doesn't parse (PdfReadError")
    assert rec.lines[1] == "WARNING statement PDF sum/statements/empty/problem.pdf dropped: it has no pages"
    assert [d.name for d in problem.glob("statements/*/problem.pdf")] == ["problem.pdf"]
