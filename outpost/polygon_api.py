"""Download packages straight from Polygon (needs the internet only during prep).

The admin pastes an API key and secret from Polygon (Settings → API keys);
Outpost lists the account's problems, finds each one's newest *ready* package
and imports it like an uploaded zip. Packages are built on Polygon itself
(``problem.buildPackage``); this only fetches them.

Signing follows Polygon's API documentation: every call carries ``apiKey``,
``time`` and ``apiSig = rand + sha512(rand/method?sorted-params#secret)``.
Polygon answers a rate-limited call with an HTML page instead of JSON, so an
HTML body means "back off and retry" (the same rule Platform-Scraper's
polygon_tests.py learned).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://polygon.codeforces.com/api"
RETRIES = 4


class PolygonError(Exception):
    pass


def sign(method: str, params: dict[str, str], secret: str, rand: str | None = None) -> str:
    rand = rand or "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    query = "&".join(f"{k}={v}" for k, v in sorted((str(k), str(v)) for k, v in params.items()))
    digest = hashlib.sha512(f"{rand}/{method}?{query}#{secret}".encode()).hexdigest()
    return rand + digest


class PolygonAPI:
    def __init__(self, key: str, secret: str, base: str | None = None, pause: float = 0.2):
        if not key or not secret:
            raise PolygonError("Polygon API key and secret are both needed")
        self.key, self.secret = key, secret
        self.base = (base or os.environ.get("OUTPOST_POLYGON_URL") or DEFAULT_URL).rstrip("/")
        self.pause = pause

    def _request(self, method: str, params: dict) -> bytes:
        for attempt in range(RETRIES):
            p = {k: str(v) for k, v in params.items()}
            p.update(apiKey=self.key, time=str(int(time.time())))
            p["apiSig"] = sign(method, p, self.secret)
            data = urllib.parse.urlencode(p).encode()
            try:
                with urllib.request.urlopen(f"{self.base}/{method}", data=data, timeout=120) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:
                with e:
                    body = e.read()  # Polygon reports API errors as JSON with a 400
            except urllib.error.URLError as e:
                raise PolygonError(f"can't reach Polygon ({e.reason}); is this machine online?") from None
            if body.lstrip()[:1] == b"<":  # an HTML page: rate-limited
                time.sleep(2 * (attempt + 1))
                continue
            time.sleep(self.pause)
            return body
        raise PolygonError(f"{method}: Polygon kept refusing (rate limit); try again in a minute")

    def call(self, method: str, **params):
        body = self._request(method, params)
        try:
            d = json.loads(body)
        except ValueError:
            raise PolygonError(f"{method}: Polygon did not answer with JSON") from None
        if d.get("status") != "OK":
            raise PolygonError(f"{method}: {d.get('comment') or 'failed'}")
        return d.get("result")

    def raw(self, method: str, **params) -> bytes:
        body = self._request(method, params)
        if body[:1] == b"{":  # an error, not a file
            d = json.loads(body)
            raise PolygonError(f"{method}: {d.get('comment') or 'failed'}")
        return body

    # -- what Outpost needs ---------------------------------------------------
    def problems(self) -> list[dict]:
        return [p for p in self.call("problems.list") if not p.get("deleted")]

    def latest_package(self, problem_id: int) -> dict | None:
        ready = [k for k in self.call("problem.packages", problemId=problem_id) if k.get("state") == "READY"]
        return max(ready, key=lambda k: int(k["id"])) if ready else None

    def download(self, problem_id: int, package: dict) -> bytes:
        """The package zip. A full build's Linux package has the tests generated;
        a standard one doesn't (the importer then generates them)."""
        if package.get("type", "standard") != "standard":
            try:
                return self.raw("problem.package", problemId=problem_id, packageId=package["id"], type="linux")
            except PolygonError:
                pass
        return self.raw("problem.package", problemId=problem_id, packageId=package["id"])

    def build_package(self, problem_id: int) -> None:
        """Ask Polygon to build a full, verified package (it takes minutes)."""
        self.call("problem.buildPackage", problemId=problem_id, full="true", verify="true")
