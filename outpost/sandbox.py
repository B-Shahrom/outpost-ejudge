"""Run untrusted programs with limits.

This is Outpost's built-in sandbox: plain POSIX process control, no external
binaries. Each run

* gets its own empty network namespace (no network at all), when the host
  allows ``unshare(CLONE_NEWNET)`` — true when running as root on Linux, or in
  a container with ``CAP_SYS_ADMIN``;
* drops to a dedicated unprivileged uid, so it cannot read the data directory
  (tests, answers, database) or touch another run's files and processes;
* is held to CPU-time, address-space, stack, file-size, process-count and
  open-file limits via ``setrlimit``, plus a wall-clock limit enforced here.

Time is CPU time (user + system), memory is peak RSS — both from ``wait4``.

It is not a full jail: the filesystem stays visible (read-only to that uid
where permissions say so). For a hostile room, run the judge in the provided
container (``docker compose``), which also has no network interface at all.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

MB = 1024 * 1024

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

    def __init__(self, box: Path, uid: int | None = None, isolate_network: bool = True):
        self.box = box
        # Dropping privileges is only possible (and only needed) as root.
        self.uid = uid if (uid is not None and os.geteuid() == 0) else None
        self.isolate_network = isolate_network

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
        stderr = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        if stderr_path and uid is not None:
            os.chown(stderr_path, uid, uid)

        def setup() -> None:  # runs in the child between fork and exec
            if isolate:
                try:
                    os.unshare(os.CLONE_NEWNET)
                except OSError:
                    pass
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
            )
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        return Running(proc=proc, limits=limits, stderr_path=stderr_path)

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
                    pending.remove(r)
                elif time.monotonic() - r.started > r.limits.wall:
                    r.killed_for_wall = True
                    _kill_group(r.proc.pid)
            if pending:
                time.sleep(0.002)
        return [r.result for r in runs]  # type: ignore[misc]

    def run(self, argv: list[str], limits: Limits, **kw) -> RunResult:
        return self.wait(self.spawn(argv, limits, **kw))[0]


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


def _classify(r: Running, status: int, usage) -> RunResult:
    lim = r.limits
    cpu_ms = int((usage.ru_utime + usage.ru_stime) * 1000)
    wall_ms = int((time.monotonic() - r.started) * 1000)
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
