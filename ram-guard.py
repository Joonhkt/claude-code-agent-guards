#!/usr/bin/env python3
"""ram-guard.py: keep a machine alive while many Claude Code agents build and test in parallel.

When used RAM goes above a threshold, kill the heaviest group of build/test processes
(dotnet, node, python, cargo, ...). The interactive Claude Code session (and tmux) is never killed.
One line per event is printed on stdout (so a monitor can follow it) and appended to a log file.

Usage:
  python3 -I ram-guard.py [--threshold-gib 24 | --threshold-percent 90] [--interval 10]
                          [--match REGEX] [--group-regex REGEX] [--protect REGEX]
                          [--protect-file FILE] [--log FILE] [--dry-run] [--once]

Groups: with --group-regex (one capture group, matched against the working directory and the
command line of a process and its ancestors) processes are grouped by the captured name, for
example 'worktrees/([^/]+)' groups by worktree. Without it, a group is the outermost matching
process and all matching processes below it. Only the heaviest group is killed per event.

Environment (options win): RAM_GUARD_THRESHOLD_GIB, RAM_GUARD_THRESHOLD_PERCENT,
RAM_GUARD_INTERVAL, RAM_GUARD_MATCH, RAM_GUARD_GROUP_REGEX, RAM_GUARD_PROTECT,
RAM_GUARD_PROTECT_FILE, RAM_GUARD_LOG.
"""
import argparse
import os
import re
import signal
import sys
import time

GIB_KB = 1048576  # KiB in a GiB
DEFAULT_MATCH = r"^(dotnet|msbuild|csc|node|npm|npx|yarn|pnpm|tsc|python[0-9.]*|pytest|cargo|rustc|gradle|java|mvn|make|cmake|cc1plus|clang|go)$"
DEFAULT_PROTECT = r"^(claude|tmux)"
ME = os.getpid()


def env(name, default=None):
    return os.environ.get("RAM_GUARD_" + name, default)


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            m[k] = int(v.split()[0])
    return m


def used_gib():
    m = meminfo()
    return (m["MemTotal"] - m["MemAvailable"]) / GIB_KB


def total_gib():
    return meminfo()["MemTotal"] / GIB_KB


def procs():
    """pid -> (ppid, cmdline, cwd, rss_kib)"""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        p = int(pid)
        try:
            with open(f"/proc/{p}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            rss = 0
            with open(f"/proc/{p}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
            try:
                cwd = os.readlink(f"/proc/{p}/cwd")
            except OSError:
                cwd = ""
            out[p] = (ppid, cmd, cwd, rss)
        except (OSError, IndexError, ValueError):
            pass
    return out


def names(cmd):
    """Basenames of the first two words of a command line (so 'node app.js' and 'node' both match)."""
    words = cmd.split(" ")[:2]
    return [os.path.basename(w) for w in words if w]


class Guard:
    def __init__(self, args):
        self.args = args
        self.match = re.compile(args.match)
        self.protect = re.compile(args.protect)
        self.group = re.compile(args.group_regex) if args.group_regex else None

    def is_protected(self, cmd):
        return "VBCSCompiler" in cmd or any(self.protect.search(n) for n in names(cmd))

    def protected_groups(self):
        """Group names listed one per line in --protect-file are never killed."""
        if not self.args.protect_file:
            return set()
        try:
            with open(os.path.expanduser(self.args.protect_file)) as f:
                return {l.strip() for l in f if l.strip() and not l.startswith("#")}
        except OSError:
            return set()

    def is_candidate(self, cmd):
        n = names(cmd)
        return bool(n) and self.match.search(n[0]) is not None

    def group_of(self, pid, ps, cache):
        """Group key of a candidate process."""
        if self.group:
            chain = []
            p = pid
            key = None
            while p in ps and p > 1:
                if p in cache:
                    key = cache[p]
                    break
                chain.append(p)
                _, cmd, cwd, _ = ps[p]
                m = self.group.search(cwd) or self.group.search(cmd)
                if m:
                    key = m.group(1)
                    break
                p = ps[p][0]
            for c in chain:
                cache[c] = key
            return key
        # No regex: outermost matching ancestor (stopping at protected processes).
        root = pid
        p = ps[pid][0]
        while p in ps and p > 1:
            cmd = ps[p][1]
            if self.is_protected(cmd):
                break
            if self.is_candidate(cmd):
                root = p
            p = ps[p][0]
        return f"tree-{root}"

    def say(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        if self.args.log:
            try:
                os.makedirs(os.path.dirname(self.args.log) or ".", exist_ok=True)
                with open(self.args.log, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def groups(self):
        ps = procs()
        cache, per = {}, {}
        skip = self.protected_groups()
        for p, (_, cmd, _, rss) in ps.items():
            if p == ME or not cmd or self.is_protected(cmd) or not self.is_candidate(cmd):
                continue
            key = self.group_of(p, ps, cache)
            if key and key not in skip:
                per.setdefault(key, []).append((p, rss, cmd))
        return ps, per

    def step(self, threshold, alert, warned):
        """One check. Returns (warned, seconds to sleep)."""
        a = self.args
        u = used_gib()
        if u >= threshold:
            _, per = self.groups()
            if per:
                key, members = max(per.items(), key=lambda kv: sum(r for _, r, _ in kv[1]))
                gib = sum(r for _, r, _ in members) / GIB_KB
                verb = "WOULD KILL" if a.dry_run else "KILL"
                if not a.dry_run:
                    for p, _, _ in members:
                        try:
                            os.kill(p, signal.SIGKILL)
                        except OSError:
                            pass
                self.say(f"{verb} group={key}: RAM {u:.1f} GiB >= {threshold:g}, "
                         f"{len(members)} processes ({gib:.1f} GiB) -> stop the matching agent")
                if a.dry_run:
                    for p, r, c in sorted(members, key=lambda m: -m[1])[:10]:
                        self.say(f"  pid {p} {r / GIB_KB:.2f} GiB {c[:100]}")
                return warned, 20
            self.say(f"RAM {u:.1f} GiB >= {threshold:g} but no matching process to kill")
            return warned, 30
        if u >= alert and a.compiler_gib > 0:
            ps = procs()
            big = [r for _, cmd, _, r in ps.values() if "VBCSCompiler" in cmd]
            if big and max(big) > a.compiler_gib * GIB_KB:
                if not a.dry_run:
                    os.system("timeout 120 dotnet build-server shutdown --vbcscompiler >/dev/null 2>&1")
                self.say(f"{'WOULD STOP' if a.dry_run else 'STOPPED'} the .NET compiler server cleanly "
                         f"({max(big) / GIB_KB:.1f} GiB), RAM {used_gib():.1f} GiB")
                return warned, a.interval
        if u >= alert and not warned:
            self.say(f"WARNING RAM {u:.1f} GiB (threshold {threshold:g})")
            return True, a.interval
        if u < alert - 1:
            warned = False
        return warned, a.interval


def parse():
    ap = argparse.ArgumentParser(description="Kill the heaviest group of build/test processes when RAM runs out.")
    ap.add_argument("--threshold-gib", type=float, default=env("THRESHOLD_GIB") and float(env("THRESHOLD_GIB")),
                    help="used RAM (GiB) above which a group is killed")
    ap.add_argument("--threshold-percent", type=float, default=float(env("THRESHOLD_PERCENT", 90)),
                    help="same, as a percentage of total RAM (default 90; ignored if --threshold-gib is set)")
    ap.add_argument("--alert-margin-gib", type=float, default=2.0, help="warn this many GiB below the threshold (default 2)")
    ap.add_argument("--interval", type=float, default=float(env("INTERVAL", 10)), help="seconds between checks (default 10)")
    ap.add_argument("--match", default=env("MATCH", DEFAULT_MATCH),
                    help="regex on the program name of killable processes (default: common build/test tools)")
    ap.add_argument("--group-regex", default=env("GROUP_REGEX"),
                    help="regex with one capture group giving the group name from a cwd or command line")
    ap.add_argument("--protect", default=env("PROTECT", DEFAULT_PROTECT),
                    help="regex on the program name that is never killed (default: claude, tmux)")
    ap.add_argument("--protect-file", default=env("PROTECT_FILE"), help="file with group names never killed, one per line")
    ap.add_argument("--compiler-gib", type=float, default=4.0,
                    help="above the alert level, shut down the .NET compiler server if it uses more than this (0 = off)")
    ap.add_argument("--log", default=env("LOG", os.path.expanduser("~/.claude/agent-guards/ram-guard.log")),
                    help="log file (default ~/.claude/agent-guards/ram-guard.log; empty = none)")
    ap.add_argument("--dry-run", action="store_true", help="only print what would be killed")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    return ap.parse_args()


def main():
    args = parse()
    threshold = args.threshold_gib if args.threshold_gib else total_gib() * args.threshold_percent / 100
    alert = max(0.0, threshold - args.alert_margin_gib)
    guard = Guard(args)
    guard.say(f"ram-guard started: threshold {threshold:g} GiB, alert {alert:g} GiB{' (dry run)' if args.dry_run else ''}")
    warned = False
    while True:
        warned, wait = guard.step(threshold, alert, warned)
        if args.once:
            return
        time.sleep(wait)


if __name__ == "__main__":
    main()
