# Outpost — design notes and roadmap

## The problem it solves

Running a contest in a classroom, a school without reliable internet, or a
regional olympiad usually means one of these:

- relying on an online judge and the venue's internet connection;
- installing a heavyweight system (ejudge, DOMjudge, CMS) on a server;
- judging by hand.

Outpost targets the case in between: **one laptop, one command, contestants on
its Wi-Fi**. The laptop is the server, the judge and the scoreboard.

## Shape

```
 contestants' laptops/phones ──Wi-Fi hotspot──▶ admin laptop
                                                ├─ web server (FastAPI, threads)
                                                ├─ judge workers × (cores − 1)   ← separate processes
                                                │    └─ sandboxed runs (uid per worker, no network)
                                                └─ data dir: outpost.db (SQLite WAL), problems/, boxes/
```

- **One data directory** holds everything: the database, imported problems and
  scratch boxes. Back it up and you have the whole contest.
- **The queue is the `submissions` table.** A worker claims a row atomically
  (`UPDATE … RETURNING`). On start-up, rows left in `judging` go back to
  `pending`, so a crash or power cut means a rejudge, not lost submissions.
- **Workers are processes**, each single-threaded with its own sandbox uid.
  That keeps fork+exec safe and gives each concurrent run its own identity.
- **Imports run in a subprocess** (`outpost import`), because they compile and
  execute package code.
- **Nothing comes from the internet.** KaTeX is vendored, there are no web
  fonts, and the UI is server-rendered with a little JS.

## Matching electicode

| electicode | Outpost |
|---|---|
| Scoring ICPC / IOI / IOI with Penalty, penalty and freeze minutes | same three modes and fields (`contests.scoring`, `penalty_minutes`, `freeze_minutes`) |
| Divisions; per-problem division access | `contests.divisions_json`, `contest_problems.divisions_json`, standings filtered per division |
| Verdict names, language ids `cpp`, `java21`, `py12` | identical strings, so exports line up |
| Subtasks (`subtaskResults`) | Polygon groups with `complete-group` / `each-test` and dependencies |
| Hidden / public standings, visible flag | `contests.visible`, `public_standings` |
| Clarifications, announcements | `clarifications` table (`user_id NULL` = announcement) |
| Rejudge | single or by filter (contest / problem / verdict / user) |

## Decisions worth remembering

- **Answers come only from the main solution.** A `wa_*` solution must never
  produce answers. (The same lesson is behind `polygon_tests.py publish` in
  Platform-Scraper.)
- **TL calibration on import.** Polygon's limits were set on Polygon's
  machines. The import reports the main solution's slowest test here and warns
  above 50% of the limit.
- **No presentation error.** electicode doesn't have one, so testlib's PE (exit
  2) counts as wrong answer.
- **ICPC stops at the first failing test, IOI runs everything.** Outside a
  contest (practice), judging is IOI-style so the full breakdown is visible.
- **IOI score = best single submission** per problem, not the maximum per
  subtask across submissions. This is easy to switch if electicode turns out
  to do otherwise.
- **One active session per contestant.** Signing in elsewhere ends the old
  session. Admins are exempt.
- **One server per data directory.** `serve` holds `serve.lock`, every worker
  holds its box's lock and exits when its parent dies (even after `kill -9`).
  Workers that outlived a server once kept judging into the new server's boxes
  and turned correct runs into compilation errors.
- **Precompiled `<bits/stdc++.h>`** is built at start-up, once per g++ version.
  It cuts a typical compile from about 2.5 s to 0.7 s, which roughly triples
  judging throughput.
- **Plain passwords are never stored.** Generated passwords live in memory
  until the admin prints the slips and clicks "forget".

## Load test

`scripts/loadtest.py` drives a running server with simulated contestants and
checks every verdict. On a 4-core machine with 3 workers, 60 contestants
submitting every ~6 s for 3 minutes (724 runs, much busier than a real room)
gave 0 server errors, every verdict as expected, page p95 under 60 ms
(sign-in, which hashes the password, about 0.5 s), and the queue drained 23 s
after the last submission.

## Roadmap

Next:
- [x] **Filesystem jail** (done 2026-10-30), built into the sandbox instead of
  depending on isolate/nsjail. It is one less thing to install, and it works
  in the Docker image with only `CAP_SYS_ADMIN`. Each run gets a private mount
  namespace: a 16 MB tmpfs root with `/usr`, `/lib*`, `/etc` and `/opt` bound
  read-only, the existing `/proc` bound (Docker refuses a fresh procfs), five
  device files and its own box writable, then `chroot`. It costs nothing
  measurable in the load test.
- [ ] PID namespace and cgroup memory accounting (the rest of what isolate does).
- [x] **mDNS** (done): `serve` announces `outpost.local`. A second Outpost on
  the same network gets `outpost-2.local`. The IP stays the main address on
  the dashboard and slips because Android browsers don't reliably resolve
  `.local`. It is off in containers, since Docker Desktop doesn't pass
  multicast to the hotspot; `OUTPOST_MDNS=1` forces it on, `0` turns it off.
- [x] **Polygon API pull** (done): Problems → *Import from Polygon*, or
  `outpost polygon list|import|build`. It is a standard-library client written
  from Polygon's API docs. It signs every call, retries when Polygon answers
  with an HTML page (rate-limited), and imports the newest READY package
  (Linux type when built full, so the tests are already generated). It warns
  when that package is older than the problem's revision. Tested against a
  fake Polygon that checks every signature; not yet run against the real one.
- [ ] **Statement PDF for the whole contest** (merge the per-problem PDFs) for
  printing.
- [ ] **Contest archive**: export or import a contest with problems,
  users and submissions as one zip.
- [ ] **Remote judge workers**: other laptops on the network join with
  `outpost worker --server http://…` to add judging capacity.

Later:
- [ ] ICPC-style **resolver**: reveal the frozen hour row by row on the projector.
- [ ] **electicode bridge**: import a group's students as users, and push final
  results or a contest's problems back and forth (via Platform-Scraper's admin
  API knowledge).
- [ ] Post-contest **plagiarism check** (token-based similarity) and a per-test
  failure heat map.
- [ ] Balloons and print requests for in-person ICPC-style events.
- [ ] Teams (several logins, one standings row).
- [ ] A built-in code editor (CodeMirror, vendored) instead of the textarea.
