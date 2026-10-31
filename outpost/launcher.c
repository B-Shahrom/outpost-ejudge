/*
 * outpost-launch FD PROGRAM [ARGS...]
 *
 * Runs PROGRAM as a fresh child and reports what it used. Outpost's sandbox
 * execs this instead of the program itself, because ru_maxrss (peak memory) is
 * kept across exec: a program forked straight from the judge's Python process
 * would be charged for the judge's own memory. Forked from this tiny process,
 * the program starts at ~1 MB.
 *
 * When PROGRAM ends, one line goes to FD (a pipe the judge holds):
 *     <exit code or -1> <signal or 0> <user us> <system us> <peak RSS KB>
 * The program never sees FD: it is closed before exec, and this process is
 * made non-dumpable so the program (same uid) can't reach it via /proc.
 *
 * PROGRAM runs with <this launcher's path>.nodump.so preloaded (nodump.c), so
 * a crash costs it nothing extra: no core dump, no kernel crash report.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 3)
        return 120;
    int fd = atoi(argv[1]);
    prctl(PR_SET_DUMPABLE, 0);
    pid_t pid = fork();
    if (pid < 0)
        return 121;
    if (pid == 0) {
        char so[4096];
        close(fd);
        if (snprintf(so, sizeof so, "%s.nodump.so", argv[0]) < (int)sizeof so)
            setenv("LD_PRELOAD", so, 1);
        execvp(argv[2], argv + 2);
        _exit(127); /* like a shell: no such program */
    }
    int status;
    struct rusage ru;
    while (wait4(pid, &status, 0, &ru) < 0)
        if (errno != EINTR)
            return 122;
    dprintf(fd, "%d %d %ld %ld %ld\n",
            WIFEXITED(status) ? WEXITSTATUS(status) : -1,
            WIFSIGNALED(status) ? WTERMSIG(status) : 0,
            (long)ru.ru_utime.tv_sec * 1000000L + ru.ru_utime.tv_usec,
            (long)ru.ru_stime.tv_sec * 1000000L + ru.ru_stime.tv_usec,
            ru.ru_maxrss);
    return 0;
}
