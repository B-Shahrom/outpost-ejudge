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
- **Memory is measured by a launcher.** `ru_maxrss` keeps a process's peak
  across `exec`, so a program forked straight from the judge's Python process
  was charged for the judge's memory: 14–23 MB for a trivial run, and memory
  limit exceeded for a correct A+B when the judge held 300 MB. Untrusted runs
  now go through `launcher.c` (built once with gcc). It forks the program from
  a ~1 MB process and reports its rusage over a pipe the program can't reach:
  the fd is closed before exec, and the launcher is non-dumpable.
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
- [x] **Programs that multiply** (done; replaces "PID namespace and cgroup
  memory"). Testing a fork bomb found that it survived its own judging: 63
  processes kept forking at the run uid's cap, so the *next* submission on
  that judge couldn't compile. In Docker, where Outpost is PID 1, orphans
  also became zombies that nobody reaped. Now:
  - after every run, a helper that becomes the run's uid kills all of its
    processes at once (`kill(-1)`), until `/proc` shows none;
  - judges are child subreapers, so orphans come back to them to be reaped;
  - compose runs a real init;
  - C++ and Python runs may have 8 processes (64 before), so a program that
    forks can't multiply its memory limit much. Java keeps 256, because the
    JVM needs threads.

  `outpost selftest` checks it, inside the container in CI too.
- *Not done, on purpose:*
  - **PID namespace.** The run would be PID 1 there and ignore signals
    that limit handling relies on. Per-run uids already stop it from
    touching other processes, and hiding them needs a fresh /proc, which
    Docker refuses.
  - **cgroup memory.** The launcher already measures a program's own
    memory, and cgroups need privileges Docker Desktop doesn't grant.
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
- [x] **Printable statements** (done): `/c/<code>/print` puts every problem on
  its own printed page (KaTeX math, examples as a table), in any statement
  language, so it works for packages without PDFs. When every problem has a
  PDF in that language, `/c/<code>/statements.pdf` merges the packages' own
  LaTeX-typeset PDFs (pypdf).
- [x] **Contest archive** (done): one zip with the contest, its problems
  (tests, statements, checker and interactor *sources*; binaries are rebuilt
  on import), users (password hashes), judged submissions and questions.
  Import never overwrites: existing problems and users are kept, and a taken
  contest code needs a new one. It runs in one transaction, so a failure
  leaves nothing behind. A *problem set only* export shares a contest without
  contestants' data. `outpost archive export|import`, or the buttons on the
  admin pages.
- [x] **Remote judges** (done): `outpost worker --server URL --token T` on
  another laptop (the dashboard shows the command and token). It claims over
  HTTP, caches each problem's tests by version, rebuilds checkers for its own
  machine, and judges with the same sandbox. Every claim, local or remote, has
  a 10-minute lease: a laptop that dies mid-judge just puts the submission
  back in the queue. A late result is dropped if the judge no longer holds
  the lease, so it can't overwrite a rejudge. Remote judges use their own
  boxes and uids, so one can even run on the server laptop. Tested with
  `serve --workers 0` and a separate worker: 227 runs from the load test,
  all judged remotely, all verdicts as expected.

Later:
- [x] ICPC-style **resolver** (done): admin contest page → *Resolver
  (projector)*. It starts from the frozen board and reveals the lowest-ranked
  team's leftmost hidden problem: the cell is highlighted, then flipped, and
  the row slides to its new place. Repeat until nothing is hidden. Space or a
  click steps; A plays it automatically. It works even after unfreezing.
  Checked in Chromium: the order it ends on equals the live standings.
- [x] **electicode bridge** (done, where it can be): Outpost stays offline, and
  the online half lives in Platform-Scraper, which already holds electicode
  sessions.
  - *Problems, electicode → Outpost:* `outpost import` takes problem folders
    as `course_backup.py --full-problems` writes them (one folder, a whole
    backup, or a zip of either): tests, limits, subtask points, and
    statements in every language (Markdown with math). A custom checker or
    interactor comes from `source.json`, which course_backup now saves
    (Platform-Scraper commit 61c5973). It is compiled with the vendored
    testlib and called the testlib way. Without its source the problem is
    refused, not judged wrongly. *Unverified:* the backup's new
    `source.json` step and electicode's checker calling convention haven't
    met the live platform yet.
  - *Users:* the CSV import is the interface
    (`username, name, division, password, team, seat`).
  - *Results back to electicode:* not possible. The platform has no endpoint
    that accepts outside results, only its own submissions. Outpost exports
    standings (CSV) and every submission (JSON) for the record.
- [x] Post-contest **similarity check** and **failure heat map** (done): the
  admin contest page → *Analysis*. Solutions are compared as normalised
  tokens (identifiers, literals, comments, layout and `#include` lines don't
  count), winnowed fingerprints like MOSS, and only within one language.
  Fingerprints in more than half of a problem's *distinct* solutions count
  as boilerplate. A copied solution counts once there, so mass copying
  doesn't hide itself. Linked solutions are grouped (n identical solutions
  are one row, not n²/2 pairs), with side-by-side comparisons. The heat map
  shows, per test, how many contestants failed it and with which verdicts.
- [x] **Balloons and print requests** (done). *Balloons*: every first solve
  of a problem by a team or contestant, oldest first, with the problem's
  colour (A red, B blue, …; named, not only shown), the seat(s), and a star
  for the first solve overall. Runners tick them off as delivered. *Print
  requests* (opt-in per contest): contestants send a file or text from the
  *Print* page, and the jury's queue opens each job as a printout headed with
  name, team and seat. Users have a seat (6th import column).
- [x] **Teams** (done): users get a team name (5th column of the import,
  or the Users page), and a contest gets a *Team contest* switch. Then
  standings have one row per team (members listed), every member's runs
  count for it, and teammates share their submissions page and can read
  each other's code, as at ICPC. Users without a team stay individual rows.
- [x] **Code editor** (done): vendored CodeMirror 5.65.16 (MIT) on the problem
  page. It highlights C++, Java and Python following the language picker,
  with line numbers, bracket matching and closing, Tab indenting with spaces,
  and line wrapping for the narrow panel. It is themed from the page's
  colours (light and dark), and a per-problem draft is kept in the browser.
  Without JavaScript the plain textarea still posts the same field.
