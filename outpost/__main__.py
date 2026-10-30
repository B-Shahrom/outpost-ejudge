"""Command line: ``python -m outpost <command>`` (or ``outpost <command>`` once installed).

    outpost serve                      web server + judge workers
    outpost admin NAME [--password P]  create (or reset) an admin account
    outpost import PACKAGE.zip         import a Polygon package
    outpost users FILE.csv             create contestants from username,name,division[,password]
    outpost selftest                   check compilers and the sandbox before a contest
    outpost judge                      judge everything pending, then exit (no server)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
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
          f"network isolation {'on' if pool.isolated else 'OFF'}")
    if settings.public_url:
        print(f"  contestants: {settings.public_url}")
    else:
        for ip in lan_addresses():
            print(f"  contestants: http://{ip}:{settings.port}/")
    print(f"  admin:       http://localhost:{settings.port}/admin")
    try:
        uvicorn.run(create_app(settings, pool), host=settings.host, port=settings.port,
                    log_level="warning")
    finally:
        pool.shutdown()
    return 0


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
    from .polygon import ImportError_, Recorder, import_package

    settings.ensure_dirs()
    db.init(settings.db_path)
    path = Path(args.package)
    try:
        import_package(path, settings, log=Recorder(echo=lambda s: print(s, flush=True)),
                       verify=not args.no_verify)
    except ImportError_ as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    finally:
        if args.delete_zip:
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

    i = sub.add_parser("import", help="import a Polygon package zip")
    i.add_argument("package")
    i.add_argument("--no-verify", action="store_true",
                   help="don't run the main solution over tests that already have answers")
    i.add_argument("--delete-zip", action="store_true", help=argparse.SUPPRESS)
    i.set_defaults(fn=cmd_import)

    u = sub.add_parser("users", help="create contestants from a CSV; prints their passwords")
    u.add_argument("csv")
    u.set_defaults(fn=cmd_users)

    t = sub.add_parser("selftest", help="check compilers and the sandbox on this machine")
    t.set_defaults(fn=cmd_selftest)

    j = sub.add_parser("judge", help="judge everything pending and exit")
    j.set_defaults(fn=cmd_judge)

    args = p.parse_args(argv)
    return args.fn(args, config.load())


if __name__ == "__main__":
    raise SystemExit(main())
