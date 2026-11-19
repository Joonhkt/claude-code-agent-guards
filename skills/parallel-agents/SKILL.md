---
name: parallel-agents
description: Run many Claude Code agents in parallel without freezing the machine or losing track. Use when launching several sub-agents or worktree jobs at once, briefing an agent, limiting concurrent builds, tracking agent progress, or merging parallel work.
---

# Run many agents safely

## 1. Write precise briefs

An agent that has to explore burns tokens and drifts. Brief it like an architect briefing a contractor:

- the goal, in the user's own words;
- the files and line ranges to touch (read them first, paste the relevant excerpts);
- the expected end state, case by case;
- the exact test commands to run (targeted, not the whole suite);
- what not to touch;
- how to report: a short final message, nothing re-narrated.

Several short agents (20 to 40 tool calls, one task each) cost less than one long agent whose context keeps growing.

## 2. Make progress visible

Each agent writes ALL its steps first, then ticks them. The status line script `agents-line.py` reads these files (default directory `~/.claude/agents-progress/`, override with `AGENTS_LINE_DIR`):

```
agents.txt             running agents, one per line:  <id>=<Label>
<id>/progress.txt      the steps of agent <id>
```

```
# started=2026-01-01T10:00:00
[x] read the code
[>] write the tests
[ ] run the suite
```

`[ ]` to do, `[>]` in progress, `[x]` done. The agent adds `<id>=<Label>` to `agents.txt` when it starts and removes it when it finishes. Give each label a distinct first word.

## 3. Limit builds and RAM

- Run every build and test through the slot wrapper so only N compile at once: `BUILD_SLOTS=2 ~/.claude/agent-guards/build-slot.sh <build or test command>`.
- Keep `ram-guard.py` running in the background: `nohup python3 -I ~/.claude/agent-guards/ram-guard.py --threshold-percent 90 >/dev/null 2>&1 &`. It kills the heaviest build group above the threshold, never the Claude Code session. Follow `~/.claude/agent-guards/ram-guard.log` (or the Monitor tool): each kill line names the agent to stop or relaunch.
- Pipe long output through `shrink.py` (see the `shrink-output` skill).
- Prefer fewer agents at once (4 to 5) over ten.

## 4. Merge one at a time

- Merge a single agent's work at a time, in a fixed order.
- Run the tests on a frozen copy (a clean checkout or worktree of the merge result), not in a tree other agents are still editing.
- Stop at the first conflict or failure and fix it before the next merge.

## 5. Keep reports short and stop chatter

- Final reports: what was done, files touched, test result, open issues. A few lines.
- An agent that keeps re-reporting, re-reading state or looping without progress: stop it, keep its progress file, relaunch with a tighter brief if the work is still needed.
