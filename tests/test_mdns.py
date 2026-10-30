"""outpost.local over mDNS, on the loopback interface (no network needed)."""
from __future__ import annotations

import pytest

pytest.importorskip("zeroconf")

from outpost import mdns  # noqa: E402


def test_announce_and_resolve():
    a = mdns.announce("127.0.0.1", 8000, "outpost-t1")
    assert a is not None and a.hostname == "outpost-t1.local"
    try:
        assert mdns.resolve("outpost-t1.local") == ["127.0.0.1"]
    finally:
        a.close()


def test_second_server_on_the_network_takes_the_next_name():
    first = mdns.announce("127.0.0.1", 8000, "outpost-t2")
    second = mdns.announce("127.0.0.1", 8001, "outpost-t2")
    try:
        assert first.hostname == "outpost-t2.local"
        assert second.hostname == "outpost-t2-2.local"
    finally:
        first.close()
        second.close()


def test_off_in_containers_unless_asked(monkeypatch):
    from outpost import config
    monkeypatch.setattr(config, "in_container", lambda: True)
    monkeypatch.delenv("OUTPOST_MDNS", raising=False)
    assert config._mdns_name() == ""
    monkeypatch.setenv("OUTPOST_MDNS", "1")
    assert config._mdns_name() == "outpost"
    monkeypatch.setattr(config, "in_container", lambda: False)
    monkeypatch.setenv("OUTPOST_MDNS", "0")
    assert config._mdns_name() == ""
