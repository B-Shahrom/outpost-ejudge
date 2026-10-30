"""Importing straight from Polygon, against tests/fake_polygon.py."""
from __future__ import annotations

import pytest

import real_packages as rp
from fake_polygon import KEY, SECRET, FakePolygon
from outpost import __main__ as cli
from outpost import db
from outpost.polygon_api import PolygonAPI, PolygonError


@pytest.fixture
def fake(tmp_path, monkeypatch):
    zips = {"close-sum": rp.points_package(tmp_path / "close-sum.zip").read_bytes()}
    f = FakePolygon(zips)
    monkeypatch.setenv("OUTPOST_POLYGON_URL", f.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    yield f
    f.close()


def test_signed_calls_and_rate_limit_retry(fake):
    api = PolygonAPI(KEY, SECRET, pause=0)
    assert [p["name"] for p in api.problems()] == ["close-sum"]
    assert fake.calls[:2] == ["problems.list", "problems.list"]  # HTML once, then retried
    assert api.latest_package(1000)["id"] == 9                  # newest READY, not RUNNING


def test_wrong_secret_is_reported(fake):
    fake.rate_limit_next = False
    with pytest.raises(PolygonError, match="Incorrect signature"):
        PolygonAPI(KEY, "nope", pause=0).problems()


def test_cli_list_and_import(fake, settings, capsys, monkeypatch):
    monkeypatch.setenv("OUTPOST_POLYGON_KEY", KEY)
    monkeypatch.setenv("OUTPOST_POLYGON_SECRET", SECRET)
    assert cli.main(["polygon", "list"]) == 0
    assert "close-sum" in capsys.readouterr().out
    assert cli.main(["polygon", "import", "close-sum"]) == 0
    out = capsys.readouterr().out
    assert "downloading package 9 (revision 7)" in out and "imported 'close-sum'" in out
    c = db.connect(settings.db_path)
    assert db.one(c, "SELECT status FROM problems WHERE slug='close-sum'")["status"] == "ready"
    assert cli.main(["polygon", "import", "no-such-problem"]) == 1
    assert cli.main(["polygon", "build", "1000"]) == 0 and fake.built == [1000]


def test_admin_page(fake, settings):
    from test_web import client  # noqa: PLC0415
    from outpost import auth
    from outpost.app import create_app
    c = db.connect(settings.db_path)
    if not db.one(c, "SELECT 1 FROM users WHERE username='pg-admin'"):
        auth.create_user(c, "pg-admin", "pw", is_admin=True)
    app = create_app(settings)
    admin = client(app, "pg-admin", "pw")
    admin.post("/admin/polygon/credentials", data={"key": KEY, "secret": SECRET})
    page = admin.get("/admin/polygon?load=1").text
    assert "close-sum" in page and 'value="1000"' in page
    assert SECRET not in page  # the secret is never sent back
    r = admin.post("/admin/polygon/import", data={"problem": "1000"})
    assert r.status_code == 303
    job = next(j for j in app.state.imports.values() if j["name"].startswith("Polygon"))
    assert job["proc"].wait(180) == 0, open(job["log_path"]).read()
    assert "imported 'close-sum'" in open(job["log_path"]).read()
