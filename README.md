# claude-code-agent-guards

Small guards for people who run **many [Claude Code](https://docs.claude.com/en/docs/claude-code) agents in parallel** on one machine: builds eat all the RAM, tool output floods the context, and you lose track of who is doing what.

Unofficial: this project is not made by Anthropic, nor affiliated with or endorsed by it.

Four independent tools, no dependency beyond `python3` (3.8+) and, for one of them, `flock`. Linux only (they read `/proc`).

| Tool | What it does | Why |
|---|---|---|
| [`ram-guard.py`](#ram-guardpy) | Kills the heaviest group of build/test processes when used RAM crosses a threshold. Never kills the Claude Code session. | Ten agents compiling at once can freeze or crash the machine. |
| [`build-slot.sh`](#build-slotsh) | Runs a command only when one of N slots is free. | Limit how many builds run at the same time. |
| [`shrink.py`](#shrinkpy) | Runs a command and prints a shortened output, keeping errors, failed test names and messages, head and tail. | Long outputs cost tokens and re-reading them costs more at every call. |
| [`agents-line.py`](#agents-linepy) | Prints a status line: running agents and their progress. | See at a glance what your agents are doing. |

## Install

```bash
git clone https://github.com/joonhkt/claude-code-agent-guards.git
cd claude-code-agent-guards
mkdir -p ~/.claude/agent-guards
install -m 755 ram-guard.py build-slot.sh shrink.py agents-line.py ~/.claude/agent-guards/
```

## ram-guard.py

Watches used RAM (`MemTotal - MemAvailable`). Above the threshold it kills the **one heaviest group** of matching processes (by default: `dotnet`, `node`, `python`, `cargo`, `gradle`, `make`... started by your agents), then waits and checks again. Processes named `claude` or `tmux` and the .NET compiler server are never killed; the compiler server is shut down cleanly instead when it grows too big.

```bash
python3 -I ram-guard.py --dry-run --once                  # print what it would do, once
python3 -I ram-guard.py --threshold-gib 24                # kill above 24 GiB used
python3 -I ram-guard.py --threshold-percent 85 --interval 5
python3 -I ram-guard.py --group-regex 'worktrees/([^/]+)' # one group per agent worktree
```

| Option | Default | Meaning |
|---|---|---|
| `--threshold-gib N` | none | Used RAM (GiB) above which a group is killed. |
| `--threshold-percent P` | `90` | Same, as a share of total RAM (used when no GiB value is set). |
| `--alert-margin-gib N` | `2` | Print a warning this far below the threshold. |
| `--interval S` | `10` | Seconds between checks. |
| `--match REGEX` | build/test tools | Program names that may be killed. |
| `--group-regex REGEX` | none | One capture group naming the agent, matched on the working directory and command line of a process and its parents. Without it, a group is a process tree. |
| `--protect REGEX` | `^(claude\|tmux)` | Program names never killed. |
| `--protect-file FILE` | none | Group names never killed, one per line. |
| `--compiler-gib N` | `4` | Shut down the .NET compiler server above this size (`0` = off). |
| `--log FILE` | `~/.claude/agent-guards/ram-guard.log` | Log file. Every event is also printed on stdout. |
| `--dry-run`, `--once` | off | Only print what it would kill; check once and exit. |

Every option has an environment variable `RAM_GUARD_<NAME>` (for example `RAM_GUARD_THRESHOLD_GIB`); options win.

**Run it in the background:**

```bash
nohup python3 -I ~/.claude/agent-guards/ram-guard.py --threshold-percent 90 >/dev/null 2>&1 &
# or as a user service: ExecStart=/usr/bin/python3 -I %h/.claude/agent-guards/ram-guard.py
tail -f ~/.claude/agent-guards/ram-guard.log
```

Inside Claude Code you can also start it as a background command and follow its output with the Monitor tool: each kill line tells you which agent to stop.

## build-slot.sh

Wraps any command in a pool of N lock slots (`flock`). When all slots are taken it waits, then runs.

```bash
BUILD_SLOTS=2 ./build-slot.sh dotnet build -m:2
./build-slot.sh npm test
```

| Variable | Default | Meaning |
|---|---|---|
| `BUILD_SLOTS` | `2` | Number of commands allowed at once. |
| `BUILD_SLOT_DIR` | `${TMPDIR:-/tmp}/build-slots-$USER` | Directory of the lock files. |
| `BUILD_SLOT_WAIT` | `15` | Seconds between tries. |

The command's exit code is returned. Locks are released if the command or the script dies.

## shrink.py

> Built on the ideas of **[Caveman](https://github.com/JuliusBrussee/caveman)** by [Julius Brussee](https://github.com/JuliusBrussee): go and star it. `shrink.py` is a dependency-free Python rewrite of its output compressors.

Runs a command and prints its output shortened; the exit code is the command's. Short outputs (under 1200 characters) are printed as is.

```bash
python3 shrink.py -- dotnet test            # run, then shorten
python3 shrink.py build.log                 # shorten a file
cat result.json | python3 shrink.py         # or standard input
```

What it keeps: first and last lines, every failed test with its message and the useful stack frames, compiler errors and warnings (once), the summary (`Passed!`, `Build succeeded`...), lines with `error`/`fail`/`exception`, and any line unlike the others. What it removes: ANSI codes, progress lines, repeated lines (`… ×40 (shrink) …`), the middle of long arrays in JSON and CSV, and passed-test noise. It also reads `.trx` and JUnit XML reports. A marker says how many lines were removed.

Tip: tell your agents, in `CLAUDE.md`, to run long commands through it: "run builds and tests as `python3 ~/.claude/agent-guards/shrink.py -- <command>`".

Adapted from [Caveman](https://github.com/JuliusBrussee/caveman) (Apache-2.0), see [NOTICE](NOTICE).

## agents-line.py

Prints one status line from plain text files, so any agent (or script) can report progress by writing a file.

```
3 agents · 12/20 · done 14h05 | Tests 2/5 · Docs 4/4 · Build 6/11
```

Files, under `~/.claude/agents-progress/` by default (change it with `AGENTS_LINE_DIR`):

```
agents.txt             running agents, one per line:  <id>=<Label>      (# = comment)
agents-<name>.txt      the list of another session, shown as "<Name> : ..."
<id>/progress.txt      the steps of agent <id>
```

`<id>/progress.txt`: the agent writes **all its steps first**, then ticks them as it goes. An optional first line `# started=<unix seconds or ISO date>` enables the end-time estimate.

```
# started=2026-01-01T10:00:00
[x] read the code
[>] write the tests
[ ] run the suite
```

`[ ]` to do, `[>]` in progress, `[x]` done. Agents with the same label are grouped; each group shows `<first word of label> <current step>/<steps>`. An agent adds `<id>=<Label>` to `agents.txt` when it starts and removes it when it finishes. A good line for your `CLAUDE.md`: "Before starting, write your steps in `~/.claude/agents-progress/<id>/progress.txt` and tick them as you go."

### Use it with claude-code-quota-bar

[claude-code-quota-bar](https://github.com/joonhkt/claude-code-quota-bar) sets one status line command. A status line may print several lines, so append this one as a second line with a small wrapper, for example `~/.claude/agent-guards/statusline.sh`:

```bash
#!/bin/bash
input=$(cat)
echo "$input" | ~/.claude/statusline/statusline-quota.sh
python3 -I ~/.claude/agent-guards/agents-line.py
```

Make it executable, then point `statusLine.command` in `~/.claude/settings.json` at it (keep `"refreshInterval": 60`, or lower it to see progress sooner):

```json
{ "statusLine": { "type": "command", "command": "~/.claude/agent-guards/statusline.sh", "refreshInterval": 30 } }
```

## Skills

Two [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) in [`skills/`](skills) teach Claude to use these tools without being told each time:

| Skill | What it does |
|---|---|
| [`shrink-output`](skills/shrink-output/SKILL.md) | Runs builds, test suites, long logs and big JSON/CSV through `shrink.py`, keeps failed test names and messages, never re-runs a suite just to see a name. |
| [`parallel-agents`](skills/parallel-agents/SKILL.md) | How to run many agents safely: precise briefs, progress files for `agents-line.py`, builds through `build-slot.sh`, `ram-guard.py` in the background, one merge at a time, short reports. |

Install (after installing the scripts above):

```bash
mkdir -p ~/.claude/skills && cp -r skills/* ~/.claude/skills/
```

## Credits

- `shrink.py` is adapted from [Caveman](https://github.com/JuliusBrussee/caveman) by [Julius Brussee](https://github.com/JuliusBrussee) (Apache License 2.0).

## Licence

MIT, see [LICENSE](LICENSE). `shrink.py` is adapted from [Caveman](https://github.com/JuliusBrussee/caveman) and stays under the Apache License 2.0: see [NOTICE](NOTICE) and [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).
