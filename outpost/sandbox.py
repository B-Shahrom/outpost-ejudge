"""Run untrusted programs with limits.

This is Outpost's built-in sandbox: plain POSIX process control, no external
binaries. Each run

* gets its own empty network namespace (no network at all), when the host
  allows ``unshare(CLONE_NEWNET)`` — true when running as root on Linux, or in
  a container with ``CAP_SYS_ADMIN``;
* drops to a dedicated unprivileged uid, so it cannot read the data directory
  (tests, answers, database) or touch another run's files and processes;
* sees a filesystem of its own (a *jail*, when mount namespaces are allowed):
  the system's programs and libraries read-only, its box, a small /tmp — and
  nothing else, so the data directory, other boxes, /home and /root don't
  exist for it;
* is held to CPU-time, address-space, stack, file-size, process-count and
  open-file limits via ``setrlimit``, plus a wall-clock limit enforced here.

Time is CPU time (user + system), memory is peak RSS — both from ``wait4``.

Without root (or CAP_SYS_ADMIN in a container) the namespaces are skipped and
the filesystem stays visible, read-only where permissions say so;
``outpost selftest`` reports which protections are on.
"""

from __future__ import annotations

import ctypes
import os
import resource
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

MB = 1024 * 1024

# What a jailed run can see besides its box: the system's programs, libraries
# and configuration, read-only. Missing ones are skipped; symlinks (merged /usr)
# are recreated as symlinks.
SYSTEM_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt")
DEVICES = ("null", "zero", "full", "random", "urandom")
JAIL_TMPFS = "size=16m,mode=755"  # the jail's root, /tmp included: small on purpose

_MS_RDONLY, _MS_NOSUID, _MS_NODEV = 1, 2, 4
_MS_REMOUNT, _MS_BIND, _MS_REC, _MS_PRIVATE = 32, 4096, 16384, 1 << 18
_libc = ctypes.CDLL(None, use_errno=True)


def _mount(src: str | None, target: str, fstype: str | None, flags: int, data: str | None = None) -> None:
    enc = (lambda s: s.encode() if s is not None else None)
    if _libc.mount(enc(src), enc(target), enc(fstype), flags, enc(data)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)


def _bind(src: str, target: str, writable: bool = False) -> None:
    _mount(src, target, None, _MS_BIND | _MS_REC)
    if not writable:  # a bind mount only becomes read-only on a remount
        _mount(None, target, None, _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _MS_NOSUID | _MS_REC)


def _enter_jail(root: str, writable: list[str], readonly: list[str]) -> None:
    """In the child: build the jail under ``root`` and chroot into it."""
    # The judge runs with umask 077 (to keep its data private); the paths we
    # create in here must stay walkable for the run's uid.
    os.umask(0o022)
    _mount(None, "/", None, _MS_REC | _MS_PRIVATE)  # nothing we mount leaks to the host
    _mount("tmpfs", root, "tmpfs", _MS_NOSUID | _MS_NODEV, JAIL_TMPFS)
    for path in SYSTEM_PATHS:
        if os.path.islink(path):
            os.symlink(os.readlink(path), root + path)
        elif os.path.isdir(path):
            os.makedirs(root + path, exist_ok=True)
            _bind(path, root + path)
    for path in readonly + writable:
        if os.path.exists(path):
            os.makedirs(root + path, exist_ok=True)
            _bind(path, root + path, writable=path in writable)
    os.makedirs(root + "/dev", exist_ok=True)
    for dev in DEVICES:
        open(f"{root}/dev/{dev}", "w").close()
        _mount(f"/dev/{dev}", f"{root}/dev/{dev}", None, _MS_BIND)
    os.makedirs(root + "/proc", exist_ok=True)
    # A fresh procfs is refused inside Docker; the existing one, bound, is not.
    _mount("/proc", root + "/proc", None, _MS_BIND | _MS_REC)
    os.makedirs(root + "/tmp", exist_ok=True)
    os.chmod(root + "/tmp", 0o1777)
    os.chroot(root)

# Messages a runtime prints when an allocation fails under the address-space
# limit. The process often dies with little resident memory, so peak RSS alone
# would call these runtime errors.
_OOM_MARKERS = (b"std::bad_alloc", b"MemoryError", b"OutOfMemoryError", b"Cannot allocate memory")


@dataclass
class Limits:
    time_ms: int                     # CPU-time limit
    memory_mb: int | None = 256      # peak RSS limit; None = unlimited
    address_space: bool = True       # also cap virtual memory at memory_mb (not for the JVM)
    rss_slack_mb: int = 0            # runtime overhead tolerated above memory_mb (JVM)
    wall_ms: int | None = None       # default: 2 * time_ms + 1000
    output_mb: int = 256             # largest file the program may write (stdout included)
    processes: int = 64              # RLIMIT_NPROC for the run's uid
    open_files: int = 256

    @property
    def wall(self) -> float:
        return (self.wall_ms if self.wall_ms is not None else 2 * self.time_ms + 1000) / 1000


@dataclass
class RunResult:
    status: str            # ok | tle | mle | re | ole
    exit_code: int | None
    signal: int | None
    time_ms: int
    wall_ms: int
    memory_kb: int
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Running:
    proc: subprocess.Popen
    limits: Limits
    stderr_path: Path | None
    started: float = field(default_factory=time.monotonic)
    result: RunResult | None = None
    killed_for_wall: bool = False
    report_fd: int | None = None     # read end of the launcher's report pipe
    uid: int | None = None           # the run's uid, when it has one of its own


def build_launcher(runtime_dir: Path, log=lambda _: None) -> Path | None:
    """Compile launcher.c into ``runtime_dir`` once (rebuilt when the source changes)."""
    import fcntl
    import hashlib
    import shutil
    src = Path(__file__).with_name("launcher.c")
    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("g++")
    if cc is None or not src.exists():
        return None
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    exe = runtime_dir / f"outpost-launch-{digest}"
    if exe.exists():
        return exe
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.chmod(0o755)  # sandboxed runs (other uids) exec it
    with open(runtime_dir / "build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if exe.exists():
            return exe
        tmp = runtime_dir / f".launch.{os.getpid()}.tmp"
        argv = [cc, "-O2", "-o", str(tmp), "-x", "c", str(src)]
        res = subprocess.run(argv, capture_output=True, text=True)
        if res.returncode != 0:
            log("could not build the launcher (memory figures will include the judge's own): "
                + res.stderr[-300:])
            return None
        tmp.chmod(0o755)
        os.replace(tmp, exe)
    return exe


def jail_available(scratch: Path) -> bool:
    """Probe once whether runs can get their own filesystem view."""
    if not hasattr(os, "unshare") or os.geteuid() != 0:
        return False
    root = scratch / "jail-probe"
    root.mkdir(parents=True, exist_ok=True)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            os.unshare(os.CLONE_NEWNS)
            _enter_jail(str(root), [], [])
            os._exit(0 if not os.path.exists(str(scratch)) else 1)  # the host is hidden
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def network_isolation_available() -> bool:
    """Probe once whether a child can be given its own network namespace."""
    if not hasattr(os, "unshare"):
        return False
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid == 0:  # pragma: no cover - child
        try:
            os.unshare(os.CLONE_NEWNET)
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


class Sandbox:
    """Spawns runs inside one box directory as one uid."""

    def __init__(self, box: Path, uid: int | None = None, isolate_network: bool = True,
                 jail: bool = False, readonly: list[Path] | None = None, launcher: Path | None = None):
        self.box = box
        # Dropping privileges is only possible (and only needed) as root.
        self.uid = uid if (uid is not None and os.geteuid() == 0) else None
        self.isolate_network = isolate_network
        self.jail = jail and os.geteuid() == 0
        self.readonly = [str(p) for p in (readonly or [])]  # extra paths a jailed run may read
        self.jail_root = box.parent / "jail"
        self.launcher = str(launcher) if launcher else None  # see launcher.c
        _become_subreaper()

    # -- box management -------------------------------------------------
    def prepare(self) -> Path:
        """Create the box empty, owned by the run uid."""
        if self.box.exists():
            self.clean()
        self.box.parent.mkdir(parents=True, exist_ok=True)
        self.box.parent.chmod(0o711)  # the run uid must be able to walk into its box
        self.box.mkdir()
        if self.uid is not None:
            os.chown(self.box, self.uid, self.uid)
        self.box.chmod(0o700)
        return self.box

    def clean(self) -> None:
        if self.uid is not None:
            # Anything the run left behind may be unreadable to us without a
            # chmod pass first; rm -rf as root handles it in one go.
            subprocess.run(["rm", "-rf", str(self.box)], check=False)
        else:
            subprocess.run(["chmod", "-R", "u+rwx", str(self.box)], check=False,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["rm", "-rf", str(self.box)], check=False)

    def give(self, path: Path) -> None:
        """Hand a file we created in the box over to the run uid."""
        if self.uid is not None:
            os.chown(path, self.uid, self.uid)

    # -- running ----------------------------------------------------------
    def spawn(self, argv: list[str], limits: Limits, *, stdin: IO | int | None = None,
              stdout: IO | int | None = None, stderr_path: Path | None = None,
              trusted: bool = False, cwd: Path | None = None,
              env: dict[str, str] | None = None) -> Running:
        """Start ``argv``. ``trusted`` runs (checkers, interactors) keep our uid."""
        uid = None if trusted else self.uid
        isolate = self.isolate_network and not trusted
        jail = self.jail and not trusted
        if jail:
            self.jail_root.mkdir(exist_ok=True)
        jail_root, writable = str(self.jail_root), [str(cwd or self.box)]
        readonly = self.readonly
        stderr = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        if stderr_path and uid is not None:
            os.chown(stderr_path, uid, uid)

        def setup() -> None:  # runs in the child between fork and exec
            if isolate:
                try:
                    os.unshare(os.CLONE_NEWNET)
                except OSError:
                    pass
            if jail:
                # No fallback here: jail_available() said it works, so a failure
                # is a real fault and the run must not go ahead unjailed.
                os.unshare(os.CLONE_NEWNS)
                _enter_jail(jail_root, writable, readonly)
                os.chdir(writable[0])
            cpu = max(1, -(-limits.time_ms // 1000)) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
            if limits.memory_mb and limits.address_space:
                cap = limits.memory_mb * MB
                resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
                resource.setrlimit(resource.RLIMIT_STACK, (cap, cap))
            else:
                resource.setrlimit(resource.RLIMIT_STACK,
                                   (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
            out = limits.output_mb * MB
            resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
            if uid is not None:
                resource.setrlimit(resource.RLIMIT_NPROC, (limits.processes, limits.processes))
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)

        report_r = report_w = None
        if self.launcher and not trusted:
            report_r, report_w = os.pipe()
            argv = [self.launcher, str(report_w), *argv]
        base_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                    "HOME": str(cwd or self.box), "TMPDIR": str(cwd or self.box)}
        if env:
            base_env.update(env)
        try:
            proc = subprocess.Popen(
                argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=stdout if stdout is not None else subprocess.DEVNULL,
                stderr=stderr, cwd=str(cwd or self.box), env=base_env,
                preexec_fn=setup, start_new_session=True, close_fds=True,
                pass_fds=(report_w,) if report_w is not None else (),
            )
        except BaseException:
            for fd in (report_r, report_w):
                if fd is not None:
                    os.close(fd)
            raise
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        if report_w is not None:
            os.close(report_w)  # only the launcher holds the write end now
        return Running(proc=proc, limits=limits, stderr_path=stderr_path, report_fd=report_r, uid=uid)

    def wait(self, *runs: Running) -> list[RunResult]:
        """Wait for every run, killing any that overstay their wall limit."""
        pending = [r for r in runs if r.result is None]
        while pending:
            for r in list(pending):
                pid, status, usage = os.wait4(r.proc.pid, os.WNOHANG)
                if pid:
                    r.proc.returncode = os.waitstatus_to_exitcode(status)
                    r.result = _classify(r, status, usage)
                    _kill_group(r.proc.pid)  # stray children of the run
                    if r.uid is not None:
                        kill_uid(r.uid)  # and anything that escaped the group (a fork bomb)
                    pending.remove(r)
                elif time.monotonic() - r.started > r.limits.wall:
                    r.killed_for_wall = True
                    _kill_group(r.proc.pid)
            if pending:
                time.sleep(0.002)
        return [r.result for r in runs]  # type: ignore[misc]

    def run(self, argv: list[str], limits: Limits, **kw) -> RunResult:
        return self.wait(self.spawn(argv, limits, **kw))[0]


def _become_subreaper() -> None:
    """Orphans of our runs come to us instead of to PID 1, so we can reap them.

    In the Docker image PID 1 is Outpost itself and never reaps: zombies
    would pile up and, since they count against the run uid's process cap,
    eventually stop that judge from even compiling.
    """
    try:
        _libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError):
        pass


def processes_of(uid: int) -> list[int]:
    out = []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid == uid:
                    out.append(int(entry.name))
            except OSError:
                pass
    return out


def kill_uid(uid: int, rounds: int = 50) -> bool:
    """Kill every process of a run's uid; True when none is left.

    A process group isn't enough: a fork bomb makes new processes while the
    group is being killed, and the survivors kept the uid at its process
    cap, so the next submission on that judge couldn't even compile. A
    helper that *becomes* the uid and calls kill(-1) signals all of them in
    one call (the uid is this judge's alone). Repeated until /proc shows none.
    """
    for _ in range(rounds):
        left = processes_of(uid)
        for pid in left:  # dead ones are zombies until reaped: they still count against the cap
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not processes_of(uid):
            return True
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                os.kill(-1, signal.SIGKILL)
            except OSError:
                pass
            os._exit(0)
        os.waitpid(pid, 0)
    return not processes_of(uid)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _stderr_tail(path: Path | None) -> bytes:
    if not path or not path.exists():
        return b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        return f.read()


def _read_report(fd: int | None) -> tuple[int | None, int | None, int, int] | None:
    """The launcher's line: (exit code, signal, cpu ms, peak KB), or None if it never wrote."""
    if fd is None:
        return None
    try:
        data = b""
        while chunk := os.read(fd, 256):
            data += chunk
    finally:
        os.close(fd)
    try:
        code, sig, user_us, sys_us, rss = (int(x) for x in data.split())
    except ValueError:
        return None
    return (code if code >= 0 else None), (sig or None), (user_us + sys_us) // 1000, rss


def _classify(r: Running, status: int, usage) -> RunResult:
    lim = r.limits
    wall_ms = int((time.monotonic() - r.started) * 1000)
    report = _read_report(r.report_fd)
    if report is not None:  # the program itself, measured by the launcher
        code, sig, cpu_ms, mem_kb = report
    else:  # no launcher, or it was killed (wall-clock limit) before reporting
        cpu_ms = int((usage.ru_utime + usage.ru_stime) * 1000)
        mem_kb = int(usage.ru_maxrss)
        sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None

    def res(kind: str, note: str = "") -> RunResult:
        return RunResult(kind, code, sig, cpu_ms, wall_ms, mem_kb, note)

    if r.killed_for_wall:
        return res("tle", "wall-clock limit")
    if sig == signal.SIGXCPU or cpu_ms > lim.time_ms:
        return res("tle")
    if sig == signal.SIGXFSZ:
        return res("ole", "output limit exceeded")
    if lim.memory_mb and mem_kb > (lim.memory_mb + lim.rss_slack_mb) * 1024:
        return res("mle")
    if sig is not None or code != 0:
        if lim.memory_mb and any(m in _stderr_tail(r.stderr_path) for m in _OOM_MARKERS):
            return res("mle")
        what = f"killed by signal {sig} ({signal.Signals(sig).name})" if sig else f"exit code {code}"
        return res("re", what)
    return res("ok")
