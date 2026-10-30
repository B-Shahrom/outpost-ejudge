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

### Windows

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
2. Download this repository (Code → Download ZIP) and unzip it.
3. Turn on **Settings → Network & internet → Mobile hotspot**.
4. Double-click **`start-windows.bat`**.

The first start builds the image, which takes a few minutes. After that it
creates the admin `jury` and prints the password, runs the self-test, and opens
the admin page. Contestants join the hotspot and open
**http://192.168.137.1:8000/**, which is Windows' hotspot address. The dashboard
shows it with a QR code, and you can change it there if your network is
different.

If contestants can't connect, run `start-windows.bat` once with right-click →
*Run as administrator*. That lets it open port 8000 in Windows Firewall.

### Linux and macOS

```bash
git clone https://github.com/B-Shahrom/outpost-ejudge && cd outpost-ejudge
./start.sh
```

The script does the same steps. When Outpost runs directly on the laptop (not
in Docker), it also announces itself as **http://outpost.local:8000/**. Windows,
macOS, iOS and Linux open that name; some Android browsers don't, so the IP is
always printed too. You can run either script again at any time:
your data is kept in a Docker volume, and the admin is only created once. To
stop, run `docker compose down`.

### Without Docker (Linux)

```bash
sudo apt install g++ openjdk-21-jdk-headless python3 python3-venv
python3 -m venv .venv && . .venv/bin/activate && pip install .
sudo -E .venv/bin/outpost admin jury
sudo -E .venv/bin/outpost selftest         # checks compilers and the sandbox
sudo -E .venv/bin/outpost serve            # http://<this machine>:8000/
```

Run it as root: that is what lets the sandbox switch each run to its own user
and give it an empty network namespace. Without root, Outpost still works but
with weaker isolation, and both the self-test and the dashboard say so.

### Before every contest

`outpost selftest` (or `docker compose exec outpost outpost selftest`) judges
known-good and known-bad programs in every language. It also checks that
submissions have no network and can't read the answers. The start scripts run
it every time.

## Running a contest

1. **Problems → Import** the Polygon package zip, or **Import from Polygon**:
   paste an API key from Polygon (Settings → API keys), tick problems, and
   Outpost downloads their newest packages. This needs the internet, so do it
   while preparing. Polygon's *Linux* or *Windows*
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
   For paper copies, **Print statements** on the contest page puts every
   problem on its own page.
5. During the contest, use **Questions** to answer clarifications (privately or
   for everyone) and post announcements. **Submissions** filters and rejudges.
   Open **Standings → Projector view** on the wall screen.
6. At the end, **Unfreeze**, then export standings (CSV) and every submission
   with its source (JSON). **Whole contest (archive)** saves everything in one
   zip: keep it as a backup, or import it on another laptop. **Problem set
   only** gives the same zip without contestants, to reuse or share.

## How judging works

| | |
|---|---|
| Languages | `cpp` (g++ `-std=gnu++20 -O2`), `java21` (any public class name), `py12` (CPython 3). Same ids as electicode |
| Verdicts | `accepted`, `wrong_answer`, `time_limit_exceeded`, `memory_limit_exceeded`, `runtime_error`, `compilation_error`, `judge_error` |
| Time | CPU time against the problem's limit; Java gets 2× the limit. A wall-clock limit catches runs that sleep |
| Memory | the program's own peak RSS against the limit (measured by a tiny launcher, so the judge's memory never counts); for C++ and Python, address space is capped at the limit too |
| ICPC contests | tests run in order and stop at the first failure ("Wrong answer on test 5") |
| IOI contests | test groups score with Polygon's rules: `complete-group` is all or nothing, `each-test` sums test points, and a group whose dependencies failed is skipped |
| Checkers | the package's testlib checker (exit codes 0/1/2/3/7, `quitp` partial points). With no checker, a token-by-token comparison |
| Interactive | the package's interactor is connected to the solution through pipes. Its verdict, then the checker's, decides the run |

Standings rules (penalty, ties, freeze) are documented at the top of
[`outpost/scoring.py`](outpost/scoring.py).

## Security model, honestly

Outpost's sandbox ([`outpost/sandbox.py`](outpost/sandbox.py)) is built in. It
needs no extra install. Every run, compiles included:

- **gets its own uid**, so it can't touch other runs or the judge's files;
- **has no network**: an empty network namespace;
- **gets its own filesystem view** (a jail: a private mount namespace and
  `chroot`). It sees only the system's programs and libraries (read-only), its
  own box and a 16 MB `/tmp`. The data directory with the tests, answers and
  database, other runs' boxes, `/home` and `/root` don't exist for it;
- **has limits** on CPU, address space, stack, file size, process count and
  open files;
- **ends with its whole process group killed.**

The namespaces need root, or `CAP_SYS_ADMIN` in a container (the provided
`docker-compose.yml` grants it). Without them the runs still get their own uid
and limits. `outpost selftest` checks every one of these protections, and the
dashboard warns if one is off.

Not done yet: there is no PID namespace, so a run can list other processes,
though not touch them. Memory is measured as peak RSS, not with cgroups.

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
