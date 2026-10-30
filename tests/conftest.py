from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import packages  # noqa: E402
from outpost import config, db, polygon  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    # Not under pytest's tmp dir: that one is 0700, and the sandbox uid has to
    # be able to walk down to its box.
    data = Path(tempfile.mkdtemp(prefix="outpost-test-"))
    data.chmod(0o711)
    os.environ["OUTPOST_DATA"] = str(data)
    os.environ["OUTPOST_WORKERS"] = "1"
    s = config.load()
    s.ensure_dirs()
    db.init(s.db_path)
    yield s
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="session")
def problems(settings, tmp_path_factory):
    """Import both test packages once; returns {slug: problem id}."""
    pkgs = tmp_path_factory.mktemp("pkgs")
    ids = {}
    for build, slug in ((packages.a_plus_b, "a-plus-b"), (packages.guess, "guess")):
        ids[slug] = polygon.import_package(build(pkgs / f"{slug}.zip"), settings,
                                           log=polygon.Recorder())
    return ids


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()
