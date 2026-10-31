"""Announce the server on the local network as ``outpost.local`` (mDNS).

Contestants can then type ``http://outpost.local:8000/`` instead of an IP.
Windows 10+, macOS, iOS and Linux desktops resolve ``.local`` names; Android
browsers are hit and miss, so the IP stays the main address everywhere and
this is shown next to it.

Inside a container the announcement can't reach the hotspot (Docker Desktop
doesn't pass multicast through), so it is off there unless asked for
(``OUTPOST_MDNS=1``, e.g. with host networking on Linux).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

SERVICE = "_http._tcp.local."


@dataclass
class Announcement:
    hostname: str          # e.g. "outpost.local"
    zc: object
    info: object

    def close(self) -> None:
        try:
            self.zc.unregister_service(self.info)
        finally:
            self.zc.close()


def announce(ip: str, port: int, name: str = "outpost", tries: int = 5) -> Announcement | None:
    """Register ``<name>.local`` -> ``ip`` (and an HTTP service). Returns None on failure.

    If another machine already holds the name (two Outposts on one network),
    the next free one is taken: ``outpost-2.local``, ``outpost-3.local``, ...

    A name counts as held when anything answers for the hostname, not only
    another Outpost: zeroconf's own probe checks just the service name.
    """
    try:
        from zeroconf import (AddressResolver, IPVersion, NonUniqueNameException,
                              ServiceInfo, Zeroconf)
    except ImportError:
        return None
    try:
        zc = Zeroconf(ip_version=IPVersion.V4Only)
    except OSError:
        return None
    for n in range(1, tries + 1):
        host = name if n == 1 else f"{name}-{n}"
        # 2 s: a responder that announced itself within the last second holds
        # its answer until that second is over (RFC 6762 section 14).
        if AddressResolver(f"{host}.local.").request(zc, 2000):
            continue
        info = ServiceInfo(
            SERVICE, f"Outpost {host}.{SERVICE}", addresses=[socket.inet_aton(ip)], port=port,
            properties={"path": "/"}, server=f"{host}.local.")
        try:
            zc.register_service(info)
        except NonUniqueNameException:
            continue
        except OSError:
            break
        return Announcement(f"{host}.local", zc, info)
    zc.close()
    return None


def resolve(hostname: str, timeout_ms: int = 3000) -> list[str]:
    """Look a ``.local`` name up the way a contestant's laptop would (for tests and selftest)."""
    from zeroconf import AddressResolver, IPVersion, Zeroconf
    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        r = AddressResolver(hostname if hostname.endswith(".") else hostname + ".")
        return r.parsed_addresses() if r.request(zc, timeout_ms) else []
    finally:
        zc.close()
