/*
 * Preloaded into every untrusted run by the launcher (LD_PRELOAD).
 *
 * A crash must cost the program nothing beyond the crash itself, but the
 * kernel can do slow work on a crashing program's CPU time:
 *
 * - When the system pipes core dumps to a helper (apport on Ubuntu,
 *   systemd-coredump, Docker Desktop's wsl-capture-crash), RLIMIT_CORE is
 *   ignored and the whole core is written into the pipe. It would also hand
 *   the program's memory, test data included, to the helper. A
 *   non-dumpable process is never dumped, but exec resets that flag, so it
 *   is set here, from inside the program.
 * - With print-fatal-signals on (Docker Desktop's kernel has it), every
 *   death by a core-dump signal prints a register dump to the console,
 *   synchronously. On Docker Desktop's console that is ~5 ms, and over a
 *   second on a busy laptop: an instant null dereference came back time
 *   limit exceeded. So a crash signal ends the program with _exit(128 + sig)
 *   instead, which the judge reads back as that signal (sandbox.py).
 *
 * SIGXCPU and SIGXFSZ are left alone: the judge tells time and output
 * limits by them. A program that installs its own handlers replaces these.
 */
#include <signal.h>
#include <sys/prctl.h>
#include <unistd.h>

static const int crash_signals[] = {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT, SIGTRAP, SIGSYS};

static void crashed(int sig) {
    _exit(128 + sig);
}

__attribute__((constructor)) static void nodump(void) {
    static char altstack[64 * 1024]; /* a stack overflow still reaches the handler */
    stack_t ss = {.ss_sp = altstack, .ss_size = sizeof altstack};
    struct sigaction sa = {.sa_handler = crashed, .sa_flags = SA_ONSTACK};
    prctl(PR_SET_DUMPABLE, 0);
    sigaltstack(&ss, 0);
    for (unsigned i = 0; i < sizeof crash_signals / sizeof *crash_signals; i++)
        sigaction(crash_signals[i], &sa, 0);
}
