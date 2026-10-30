# Outpost

**An offline ejudge for programming contests.** One laptop runs Outpost, and every
submission is compiled and judged on that machine. Contestants join the laptop's
Wi-Fi hotspot (or any shared network), open the address on the admin dashboard,
and compete. No internet is needed during the contest: no cloud judge, no CDN, no
accounts anywhere else.

- **Imports Polygon packages** with tests, groups and points, testlib checkers,
  interactors, generators, the main solution and multi-language statements.
- **Scores like electicode:** ICPC (penalty and freeze), IOI, and IOI with
  penalty, with divisions inside one contest. It uses the same verdict and
  language names.
- **Sandboxed runs:** each submission runs as its own unprivileged user with
  CPU, memory, output and process limits, and without network access. It
  can't read tests, answers or other people's code.
- **Built for the room:** printable login slips, a QR code for the address,
  one active session per account, live verdicts, clarifications and
  announcements, a projector view of the standings, CSV and JSON export.

## Quick start

### Docker (Linux, Windows via WSL2, macOS)

```bash
git clone https://github.com/b-shahrom/outpost-ejudge && cd outpost-ejudge
docker compose up -d --build
docker compose exec outpost outpost admin jury        # prints the admin password
```

Open `http://localhost:8000/admin`. The dashboard shows the address contestants
should type.

> **On Windows and macOS** Docker runs inside a VM, so `network_mode: host`
> does not expose port 8000 to the hotspot. Replace it with `ports: ["8000:8000"]`
> in `docker-compose.yml` and allow port 8000 through the firewall. The
> dashboard will show the VM's address, so give contestants the laptop's
> hotspot IP instead (`ipconfig` on Windows).

### Directly on Linux

```bash
sudo apt install g++ openjdk-21-jdk-headless python3 python3-venv
python3 -m venv .venv && . .venv/bin/activate && pip install .
sudo -E .venv/bin/outpost admin jury
sudo -E .venv/bin/outpost serve            # http://<this machine>:8000/
```

Run it as root: that is what lets the sandbox switch each run to its own user
and give it an empty network namespace. Without root, Outpost still works but
with weaker isolation, and the dashboard warns about it.

## Running a contest

1. **Problems → Import** the Polygon package zip. Polygon's *Linux* or *Windows*
   package includes the tests. A *Standard* package also works: any missing test
   is built by running the package's generator with that test's command. Missing
   answers come from the **main** solution only. Every import runs the main
   solution on every test and reports its slowest time *on this machine*, so you
   can see whether the time limit still fits this hardware.
2. **Users → Add contestants:** one line per person
   (`username, full name, division[, password]`). Missing passwords are
   generated, and **Print login slips** gives a page of cut-out slips.
   `outpost users people.csv` does the same from the command line.
3. **Contests → New contest:** start time, length, scoring (ICPC / IOI / IOI
   with penalty), penalty minutes, freeze minutes, divisions. Then add problems.
   A problem can be limited to some of the contest's divisions.
4. Turn on the laptop's hotspot and show the QR code from the dashboard.
5. During the contest, use **Questions** to answer clarifications (privately or
   for everyone) and post announcements. **Submissions** filters and rejudges.
   Open **Standings → Projector view** on the wall screen.
6. At the end, **Unfreeze**, then export standings (CSV) and every submission
   with its source (JSON).

## How judging works

| | |
|---|---|
| Languages | `cpp` (g++ `-std=gnu++20 -O2`), `java21` (any public class name), `py12` (CPython 3). Same ids as electicode |
| Verdicts | `accepted`, `wrong_answer`, `time_limit_exceeded`, `memory_limit_exceeded`, `runtime_error`, `compilation_error`, `judge_error` |
| Time | CPU time against the problem's limit; Java gets 2× the limit. A wall-clock limit catches runs that sleep |
| Memory | peak RSS against the limit; for C++ and Python, address space is capped at the limit too |
| ICPC contests | tests run in order and stop at the first failure ("Wrong answer on test 5") |
| IOI contests | test groups score with Polygon's rules: `complete-group` is all or nothing, `each-test` sums test points, and a group whose dependencies failed is skipped |
| Checkers | the package's testlib checker (exit codes 0/1/2/3/7, `quitp` partial points). With no checker, a token-by-token comparison |
| Interactive | the package's interactor is connected to the solution through pipes. Its verdict, then the checker's, decides the run |

Standings rules (penalty, ties, freeze) are documented at the top of
[`outpost/scoring.py`](outpost/scoring.py).

## Security model, honestly

Outpost's sandbox ([`outpost/sandbox.py`](outpost/sandbox.py)) uses plain POSIX
process control. Every run:

- gets its own uid,
- has no network (an empty network namespace),
- has rlimits on CPU, address space, stack, file size, process count and open
  files,
- has its whole process group killed when it ends.

The data directory is private to root, so a submission can't read the answers;
the tests check this. The filesystem stays visible read-only (no chroot or mount
namespace yet), so a submission can read world-readable system files. On a
contest laptop that is acceptable. A full jail (isolate/nsjail backend) is on the
roadmap.

## Development

```bash
pip install -e '.[dev]'
sudo -E python -m pytest      # tests need g++; Java tests need a JDK; uid isolation needs root
```

The layout is small on purpose:

| File | What it does |
|---|---|
| `outpost/polygon.py` | reads the package, generates missing tests and answers, converts statements |
| `outpost/latex.py` | turns statement LaTeX into HTML (KaTeX renders the math in the browser) |
| `outpost/sandbox.py` | runs untrusted code with limits |
| `outpost/judge.py` | compile, run, check, score subtasks |
| `outpost/worker.py` | judge processes taking submissions off the SQLite queue |
| `outpost/scoring.py` | standings (ICPC / IOI / IOI with penalty, freeze, divisions) |
| `outpost/app.py`, `templates/` | contestant and admin web UI (FastAPI + Jinja2, no build step) |

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and the roadmap.

## License

MIT. KaTeX (vendored in `outpost/static/vendor/katex`) is MIT-licensed too.
