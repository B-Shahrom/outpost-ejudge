"""Command line: ``python -m outpost <command>`` (or ``outpost <command>`` once installed).

    outpost serve                      web server + judge workers
    outpost admin NAME [--password P]  create (or reset) an admin account
    outpost import PACKAGE.zip         import a Polygon package
    outpost users FILE.csv             create contestants from username,name,division[,password]
    outpost polygon list|import|build  work with your Polygon account (needs the internet)
    outpost archive export|import ...  a whole contest as one zip (move, back up, share)
    outpost worker --server URL --token T  judge for another Outpost over the network
    outpost selftest                   check compilers and the sandbox before a contest
    outpost judge                      judge everything pending, then exit (no server)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import sys
from pathlib import Path

from . import __version__, auth, config, db


def cmd_serve(args, settings) -> int:
    import uvicorn

    from .app import create_app, lan_addresses
    from .worker import WorkerPool

    settings = dataclasses.replace(settings, host=args.host or settings.host,
                                   port=args.port or settings.port)
    settings.ensure_dirs()
    lock = _lock_data_dir(settings)  # noqa: F841 - held while serving
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    if not db.one(conn, "SELECT 1 FROM users WHERE is_admin=1"):
        print("No admin account yet. Create one first:  outpost admin <name>", file=sys.stderr)
        return 1
    workers = args.workers if args.workers is not None else settings.workers
    pool = WorkerPool(settings, workers)
    pool.start()
    print(f"Outpost {__version__} — {workers} judge worker(s), "
          f"network isolation {'on' if pool.isolated else 'OFF'}, "
          f"filesystem jail {'on' if pool.jailed else 'OFF'}")
    if settings.public_url:
        print(f"  contestants: {settings.public_url}")
    elif config.in_container():
        print("  contestants: set the address on the admin dashboard (a container can't see it)")
    else:
        for ip in lan_addresses():
            print(f"  contestants: http://{ip}:{settings.port}/")
    announced = _announce(settings, lan_addresses)
    if announced:
        print(f"  also:        http://{announced.hostname}:{settings.port}/  (mDNS)")
    print(f"  admin:       http://localhost:{settings.port}/admin")
    try:
        uvicorn.run(create_app(settings, pool, announced.hostname if announced else None),
                    host=settings.host, port=settings.port, log_level="warning")
    finally:
        pool.shutdown()
        if announced:
            announced.close()
    return 0


def _announce(settings, lan_addresses):
    """Announce <name>.local for the address contestants use, if mDNS is on."""
    import ipaddress
    from urllib.parse import urlparse

    from .mdns import announce
    if not settings.mdns_name:
        return None
    host = urlparse(settings.public_url).hostname if settings.public_url else None
    try:
        ip = str(ipaddress.IPv4Address(host)) if host else None
    except ValueError:
        ip = None
    ip = ip or next((a for a in lan_addresses() if not a.startswith("127.")), None)
    return announce(ip, settings.port, settings.mdns_name) if ip else None


def _lock_data_dir(settings):
    """One server per data directory: a second one would share judge boxes."""
    import fcntl
    import os
    handle = open(settings.data_dir / "serve.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        pid = handle.read().strip() or "?"
        raise SystemExit(f"Outpost is already running on {settings.data_dir} (pid {pid}). "
                         f"Stop it first, or use another OUTPOST_DATA.") from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def cmd_admin(args, settings) -> int:
    settings.ensure_dirs()
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    password = args.password or auth.generate_password(12)
    row = db.one(conn, "SELECT id FROM users WHERE username=?", (args.name,))
    if args.if_missing and db.one(conn, "SELECT 1 FROM users WHERE is_admin=1"):
        return 0  # the start scripts call this every time; only the first run creates one
    if row:
        conn.execute("UPDATE users SET password_hash=?, is_admin=1 WHERE id=?",
                     (auth.hash_password(password), row["id"]))
        print(f"Reset admin {args.name!r}.")
    else:
        auth.create_user(conn, args.name, password, display_name=args.name, is_admin=True)
        print(f"Created admin {args.name!r}.")
    if not args.password:
        print(f"Password: {password}")
    return 0


def cmd_import(args, settings) -> int:
    from .polygon import ImportError_, Recorder, import_path

    settings.ensure_dirs()
    db.init(settings.db_path)
    path = Path(args.package)
    try:
        import_path(path, settings, log=Recorder(echo=lambda s: print(s, flush=True)),
                    verify=not args.no_verify)
    except ImportError_ as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    finally:
        if args.delete_zip and path.is_file():
            path.unlink(missing_ok=True)
    return 0


def cmd_users(args, settings) -> int:
    settings.ensure_dirs()
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    out = csv.writer(sys.stdout)
    out.writerow(["username", "name", "division", "password"])
    with open(args.csv, newline="", encoding="utf-8") as f:
        for rec in csv.reader(f):
            rec = [x.strip() for x in rec]
            if not rec or not rec[0] or rec[0].lower() == "username":
                continue
            name = rec[1] if len(rec) > 1 else ""
            division = rec[2] if len(rec) > 2 else ""
            password = rec[3] if len(rec) > 3 and rec[3] else auth.generate_password()
            if db.one(conn, "SELECT 1 FROM users WHERE username=?", (rec[0],)):
                print(f"skipped existing user {rec[0]}", file=sys.stderr)
                continue
            auth.create_user(conn, rec[0], password, display_name=name, division=division)
            out.writerow([rec[0], name, division, password])
    return 0


def _polygon(settings):
    from .polygon_api import PolygonAPI
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    key = os.environ.get("OUTPOST_POLYGON_KEY") or db.get_setting(conn, "polygon_key")
    secret = os.environ.get("OUTPOST_POLYGON_SECRET") or db.get_setting(conn, "polygon_secret")
    if not key or not secret:
        raise SystemExit("No Polygon API key: save one on the admin's Polygon page, or set "
                         "OUTPOST_POLYGON_KEY and OUTPOST_POLYGON_SECRET.")
    return PolygonAPI(key, secret)


def cmd_polygon(args, settings) -> int:
    from .polygon import ImportError_, Recorder, import_package
    from .polygon_api import PolygonError

    settings.ensure_dirs()
    say = lambda s: print(s, flush=True)  # noqa: E731
    try:
        api = _polygon(settings)
        problems = api.problems()
        if args.action == "list":
            for p in sorted(problems, key=lambda p: p["name"]):
                pkg = p.get("latestPackage")  # newest packaged revision, if any
                print(f"{p['id']:>8}  {p['name']:<30} rev {p.get('revision', '?'):<4} "
                      f"package: {('rev ' + str(pkg)) if pkg else 'none'}")
            return 0
        failed = 0
        for ref in args.problems:
            match = [p for p in problems if str(p["id"]) == ref or p["name"] == ref]
            if not match:
                say(f"{ref}: no such problem in this Polygon account")
                failed += 1
                continue
            p = match[0]
            if args.action == "build":
                api.build_package(p["id"])
                say(f"{p['name']}: Polygon is building a package")
                continue
            pkg = api.latest_package(p["id"])
            if pkg is None:
                say(f"{p['name']}: no ready package. Build one in Polygon (or `outpost polygon build {p['id']}`).")
                failed += 1
                continue
            if int(pkg.get("revision", 0)) < int(p.get("revision", 0)):
                say(f"{p['name']}: WARNING the newest package is revision {pkg.get('revision')}, the problem "
                    f"is at {p.get('revision')}; importing the package anyway")
            say(f"{p['name']}: downloading package {pkg['id']} (revision {pkg.get('revision')})…")
            zip_path = settings.uploads_dir / f"polygon-{p['id']}-{pkg['id']}.zip"
            zip_path.write_bytes(api.download(p["id"], pkg))
            try:
                import_package(zip_path, settings, log=Recorder(echo=say))
            except ImportError_ as e:
                say(f"{p['name']}: FAILED: {e}")
                failed += 1
            finally:
                zip_path.unlink(missing_ok=True)
        return 1 if failed else 0
    except PolygonError as e:
        say(f"FAILED: {e}")
        return 1


def cmd_archive(args, settings) -> int:
    from .archive import ArchiveError, export_contest, import_contest
    settings.ensure_dirs()
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        if args.action == "export":
            out = Path(args.file or f"{args.target}.outpost.zip")
            n = export_contest(conn, settings, args.target, out, include_people=not args.problems_only)
            print(f"wrote {out}: {n['problems']} problem(s), {n['users']} user(s), "
                  f"{n['submissions']} submission(s)")
        else:
            code = import_contest(conn, settings, Path(args.target), new_code=args.code)
            print(f"imported as contest {code!r}")
    except ArchiveError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_worker(args, settings) -> int:
    from .remote import serve_forever
    serve_forever(args.server, args.token, settings, args.workers or settings.workers)
    return 0


def cmd_selftest(args, settings) -> int:
    from .selftest import run
    return run()


def cmd_judge(args, settings) -> int:
    from .worker import drain

    settings.ensure_dirs()
    db.init(settings.db_path)
    print(f"judged {drain(settings)} submission(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outpost", description="Offline ejudge for programming contests.")
    p.add_argument("--version", action="version", version=f"outpost {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web server and the judge")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--workers", type=int, help="judge processes (default: cores - 1)")
    s.set_defaults(fn=cmd_serve)

    a = sub.add_parser("admin", help="create or reset an admin account")
    a.add_argument("name")
    a.add_argument("--password", help="default: generate one and print it")
    a.add_argument("--if-missing", action="store_true",
                   help="do nothing if any admin exists (for start scripts)")
    a.set_defaults(fn=cmd_admin)

    i = sub.add_parser("import", help="import a Polygon package zip, or electicode problems "
                                      "backed up by Platform-Scraper (folder or zip)")
    i.add_argument("package")
    i.add_argument("--no-verify", action="store_true",
                   help="don't run the main solution over tests that already have answers")
    i.add_argument("--delete-zip", action="store_true", help=argparse.SUPPRESS)
    i.set_defaults(fn=cmd_import)

    u = sub.add_parser("users", help="create contestants from a CSV; prints their passwords")
    u.add_argument("csv")
    u.set_defaults(fn=cmd_users)

    pg = sub.add_parser("polygon", help="list, import or build problems from your Polygon account")
    pg.add_argument("action", choices=["list", "import", "build"])
    pg.add_argument("problems", nargs="*", help="problem ids or names (import, build)")
    pg.set_defaults(fn=cmd_polygon)

    ar = sub.add_parser("archive", help="export a contest to one zip, or import one")
    ar.add_argument("action", choices=["export", "import"])
    ar.add_argument("target", help="contest code (export) or archive file (import)")
    ar.add_argument("file", nargs="?", help="export: output file (default <code>.outpost.zip)")
    ar.add_argument("--code", help="import: create the contest under this code instead")
    ar.add_argument("--problems-only", action="store_true",
                    help="export: leave users, submissions and questions out")
    ar.set_defaults(fn=cmd_archive)

    w = sub.add_parser("worker", help="judge for another Outpost over the network")
    w.add_argument("--server", required=True, help="the server's address, e.g. http://192.168.137.1:8000/")
    w.add_argument("--token", required=True, help="from the server's admin dashboard")
    w.add_argument("--workers", type=int, help="judge processes (default: cores - 1)")
    w.set_defaults(fn=cmd_worker)

    t = sub.add_parser("selftest", help="check compilers and the sandbox on this machine")
    t.set_defaults(fn=cmd_selftest)

    j = sub.add_parser("judge", help="judge everything pending and exit")
    j.set_defaults(fn=cmd_judge)

    args = p.parse_args(argv)
    return args.fn(args, config.load())


if __name__ == "__main__":
    raise SystemExit(main())
