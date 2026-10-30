"""Simulate a room of contestants against a running Outpost.

    python scripts/loadtest.py --url http://127.0.0.1:8000 --users u00..u59 --password pw \\
        --contest load --minutes 3

Each simulated contestant signs in, reads the problems, submits a mix of
correct, wrong and slow solutions (respecting the submit cooldown), polls its
verdicts and refreshes the standings. At the end it prints page latencies, any
server errors, and how long the judge queue took to drain.

The sources are for the test packages (``A`` = array-sum, ``B`` = A+B), so the
expected verdict of each run is known and checked.
"""

from __future__ import annotations

import argparse
import random
import statistics
import threading
import time
from collections import defaultdict

import httpx

SOURCES = {
    "A": [
        ("accepted", "#include <bits/stdc++.h>\nusing namespace std;\nint main(){int n;cin>>n;long long s=0;"
                     "for(int i=0;i<n;i++){long long x;cin>>x;s+=x;}cout<<s<<\"\\n\";}"),
        ("wrong_answer", "#include <bits/stdc++.h>\nusing namespace std;\nint main(){int n;cin>>n;int s=0;"
                         "for(int i=0;i<n;i++){int x;cin>>x;s+=x;}cout<<s<<\"\\n\";}"),
        ("time_limit_exceeded", "#include <bits/stdc++.h>\nusing namespace std;\nint main(){int n;cin>>n;"
                                "vector<long long>a(n);for(auto&x:a)cin>>x;long long s=0;"
                                "for(int i=0;i<n;i++)for(int j=0;j<n;j++)s+=(i==j)*a[j];cout<<s;}"),
        ("compilation_error", "int main( {"),
        ("accepted", "n = int(input())\nprint(sum(map(int, input().split())))\n", "py12"),
    ],
    "B": [
        ("accepted", "#include <bits/stdc++.h>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"),
        ("wrong_answer", "#include <bits/stdc++.h>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;}"),
        ("runtime_error", "int main(){int*p=0;return *p;}"),
    ],
}


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.errors: list[str] = []
        self.submitted: list[tuple[str, int, str]] = []  # (user, submission id, expected verdict)

    def time(self, name: str, seconds: float) -> None:
        with self.lock:
            self.latency[name].append(seconds)

    def error(self, msg: str) -> None:
        with self.lock:
            self.errors.append(msg)


def contestant(base: str, user: str, password: str, contest: str, until: float, st: Stats, seed: int):
    rnd = random.Random(seed)
    cl = httpx.Client(base_url=base, follow_redirects=False, timeout=60)

    def get(path: str, name: str) -> httpx.Response | None:
        t = time.monotonic()
        try:
            r = cl.get(path)
        except httpx.HTTPError as e:
            st.error(f"{user} GET {path}: {e!r}")
            return None
        st.time(name, time.monotonic() - t)
        if r.status_code >= 500:
            st.error(f"{user} GET {path}: {r.status_code}")
        return r

    t = time.monotonic()
    r = cl.post("/login", data={"username": user, "password": password})
    st.time("login", time.monotonic() - t)
    if r.status_code != 303 or r.headers.get("location") == "/login":
        st.error(f"{user}: login failed ({r.status_code})")
        return
    get(f"/c/{contest}", "contest")
    last_submit = 0.0
    while time.monotonic() < until:
        action = rnd.random()
        if action < 0.35:
            get(f"/c/{contest}/p/{rnd.choice('AB')}", "problem")
        elif action < 0.55:
            get(f"/c/{contest}/standings", "standings")
        elif action < 0.8:
            get(f"/api/c/{contest}/my-submissions", "poll")
        elif time.monotonic() - last_submit > 6:
            label = rnd.choice("AB")
            expected, src, *lang = rnd.choice(SOURCES[label])
            t = time.monotonic()
            r = cl.post(f"/c/{contest}/submit",
                        data={"label": label, "language": lang[0] if lang else "cpp", "source": src})
            st.time("submit", time.monotonic() - t)
            last_submit = time.monotonic()
            if r.status_code != 303 or not r.headers.get("location", "").endswith("/submissions"):
                st.error(f"{user} submit: {r.status_code} {r.headers.get('location')}")
            else:
                newest = cl.get(f"/api/c/{contest}/my-submissions").json()[0]["id"]
                with st.lock:
                    st.submitted.append((user, newest, expected))
        time.sleep(rnd.uniform(0.5, 3.0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--users", default="u00..u59", help="prefix + range, e.g. u00..u59")
    ap.add_argument("--password", default="pw")
    ap.add_argument("--contest", default="load")
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--admin", default="jury:jurypass")
    args = ap.parse_args()

    lo, hi = args.users.split("..")
    prefix = lo.rstrip("0123456789")
    width = len(lo) - len(prefix)
    users = [f"{prefix}{i:0{width}d}" for i in range(int(lo[len(prefix):]), int(hi[len(prefix):]) + 1)]

    st = Stats()
    until = time.monotonic() + args.minutes * 60
    threads = [threading.Thread(target=contestant, args=(args.url, u, args.password, args.contest, until, st, n))
               for n, u in enumerate(users)]
    started = time.monotonic()
    for th in threads:
        th.start()
        time.sleep(0.05)  # a room doesn't sign in in the same millisecond
    for th in threads:
        th.join()
    load_end = time.monotonic()

    # Wait for the judge to finish what was submitted.
    admin = httpx.Client(base_url=args.url, timeout=60)
    name, pw = args.admin.split(":")
    admin.post("/login", data={"username": name, "password": pw})
    while True:
        page = admin.get("/admin/submissions", params={"contest": args.contest, "verdict": "pending"}).text
        left = int(page.split(" found")[0].rsplit(">", 1)[-1])
        if left == 0:
            break
        time.sleep(1)
    drained = time.monotonic()

    # Check every verdict against what that source should get.
    wrong = []
    export = admin.get(f"/admin/contests/{args.contest}/export.json").json()["submissions"]
    by_id = {s["id"]: s for s in export}
    for user, sid, expected in st.submitted:
        got = by_id.get(sid, {}).get("verdict")
        if got != expected:
            wrong.append(f"#{sid} {user}: expected {expected}, got {got}")

    print(f"{len(users)} contestants for {args.minutes:g} min, {len(st.submitted)} submissions")
    print(f"queue drained {drained - load_end:.1f} s after the last submission window closed "
          f"({drained - started:.0f} s total)")
    for name, xs in sorted(st.latency.items()):
        xs = sorted(xs)
        p95 = xs[int(len(xs) * 0.95) - 1] if len(xs) > 1 else xs[0]
        print(f"  {name:10s} n={len(xs):5d}  median {statistics.median(xs) * 1000:6.0f} ms  "
              f"p95 {p95 * 1000:6.0f} ms  max {xs[-1] * 1000:6.0f} ms")
    print(f"server errors: {len(st.errors)}")
    for e in st.errors[:20]:
        print("  " + e)
    print(f"verdicts not as expected: {len(wrong)}")
    for w in wrong[:20]:
        print("  " + w)
    return 1 if st.errors or wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
