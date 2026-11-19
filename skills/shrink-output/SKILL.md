---
name: shrink-output
description: Run builds, test suites, long logs and big JSON/CSV/YAML output through shrink.py so only the useful lines reach the context. Use when running dotnet/npm/cargo/gradle/pytest builds or tests, tailing long logs, or reading large structured output.
---

# Shrink long output

Long command output costs tokens now and again at every later call, because the whole history is re-read. Shorten it before it enters the context.

## How

Run the command through `shrink.py`. It prints the shortened output and exits with the command's own exit code.

```bash
python3 ~/.claude/agent-guards/shrink.py -- <command>      # run, then shorten
python3 ~/.claude/agent-guards/shrink.py build.log          # shorten a file
cat result.json | python3 ~/.claude/agent-guards/shrink.py  # or standard input
```

If it is not installed there, use `shrink.py` from the claude-code-agent-guards checkout (`python3 /path/to/shrink.py -- <command>`). If it is not available at all, filter by hand: keep the first and last lines, every `error`/`fail`/`exception` line, and the summary line (`grep -E`, `tail -n 40`).

## What it keeps and removes

- Keeps: first and last lines, each failed test with its name and message, useful stack frames, compiler errors and warnings (once), the summary (`Passed!`, `Build succeeded`), anything unlike the other lines.
- Removes: ANSI codes, progress lines, repeated lines, the middle of long JSON/CSV arrays, passed-test noise. It also reads `.trx` and JUnit XML reports.
- Output under about 1200 characters is printed as is.

## Rules

- Use it for builds, test suites, long logs, big JSON/CSV/YAML. Not needed for short commands.
- Failed test names and messages must survive any filtering you add. Never cut them with `head` or `tail` alone.
- Never re-run a suite just to see a test name. Read the shortened output (or the report file) you already have.
- If the exit code is non-zero, read the kept error lines before doing anything else.

## Credit

`shrink.py` is adapted from [Caveman](https://github.com/JuliusBrussee/caveman) by Julius Brussee (Apache-2.0). See the repository `NOTICE`.
