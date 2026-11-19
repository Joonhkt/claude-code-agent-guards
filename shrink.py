#!/usr/bin/env python3
"""shrink.py: shorten long command output (build, tests, logs, JSON, CSV, YAML) before an LLM reads it.

Adapted from Caveman (Apache-2.0, Julius Brussee, https://github.com/JuliusBrussee/caveman,
engine/compressors: terminal, log, repetition, redundancy, json, tabular, testreport).
Rewritten in Python with no dependency, no network and no retrieval store. See NOTICE.

Usage:
  shrink.py -- <command...>  run the command (stdout+stderr), print the shortened output,
                             exit with the command's exit code
  shrink.py [file]           shorten a file (or standard input)

Never removed: the name and message of every failed test, "Error Message" blocks, useful stack
frames (lines "in file:line N"), compiler errors and warnings in full, and the summary
(Passed!/Failed!, Build succeeded...). The first and last lines are always kept.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

MIN_CHARS = 1200          # below this: printed as is (ANSI removed)
MIN_LINES = 12
KEEP_HEAD = 3
KEEP_TAIL = 3
CONTAINMENT = 0.9         # redundancy.go : redundantUnitContainment
MAX_CLASSES = 64          # redundancyMaxClasses
MAX_PROMOTIONS = 128      # redundancyMaxPromotions
REP_MIN_RUN = 3           # repetition.go : minRun
ARRAY_MIN = 8             # json.go : arrayThreshold ; tabular.go : 8 lignes
ARRAY_FIRST, ARRAY_LAST = 3, 2
STACK_FRAMES = 4          # "in file:line" frames kept per failure
STACK_HEAD, STACK_TAIL = 3, 2  # testreport.go : trimStack

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# terminal.go + log.go: lines that carry signal
# (English and French patterns: dotnet/MSBuild/NUnit print localized messages on French systems)
IMPORTANT_RE = re.compile(
    r"(?i)(\b(ERROR|ERR|FATAL|PANIC|EXCEPTION|TRACEBACK|FAIL|FAILED|FAILURE|WARN|WARNING|CRIT|CRITICAL"
    r"|ALERT|EMERG|EMERGENCY|DENIED|REJECTED|TIMEOUT|ÉCHEC|ECHEC|ERREUR|AVERTISSEMENT)\b"
    r"|[A-Za-z]+Error\b|[A-Za-z]+Exception\b|^\s+File \"|\.go:\d+|:\d+:\d+:|^\s*--->|caused by"
    r"|\bexit (status|code) [1-9])")
# dotnet / NUnit / MSBuild: always kept in full
DIAG_RE = re.compile(r"\b(error|warning|erreur|avertissement)\s+[A-Z]{1,4}\d{2,5}\b|: (error|warning) ", re.I)
FAILED_TEST_RE = re.compile(r"^\s*(Failed|Échec|Echec|Error|Erreur)\s+\S+.*(\[[^\]]*\])?\s*$")
SUMMARY_RE = re.compile(
    r"(?i)(^\s*(Passed|Failed|Réussi|Échec|Echec)!|Total tests|Test Run (Successful|Failed|Aborted)"
    r"|^\s*(Passed|Failed|Skipped|Réussi|Échoué|Ignoré)\s*:\s*\d|Build (succeeded|FAILED)"
    r"|La génération a (réussi|échoué)|Générer a (réussi|échoué)|^\s*\d+ (Warning|Error|Avertissement|Erreur)\(s\)"
    r"|Time Elapsed|Temps écoulé|^\s*Résultats?|Duration:|Durée\s*:|tests? (passed|failed)|\d+ (passed|failed)"
    r"|^Ran \d+ tests?|^(OK|FAILED)( \(|$))")
MSG_START_RE = re.compile(r"^\s*(Error Message|Message d'erreur)\s*:", re.I)
STACK_START_RE = re.compile(r"^\s*(Stack Trace|Arborescence des appels de procédure)\s*:", re.I)
FRAME_RE = re.compile(r"^\s*(at|à)\s")
USEFUL_FRAME_RE = re.compile(r"\s(in|dans)\s+\S.*:(line|ligne)\s+\d+")
SECTION_END_RE = re.compile(r"^\s*(Standard Output Messages|Standard Error Messages|Messages de sortie standard)\s*:", re.I)
TOKEN_RE = re.compile(r"[^\W_]+")
DIGITS_RE = re.compile(r"\d+")
JSON_ERROR_RE = re.compile(r"(?i)\b(error|errors|exception|failed|failure|critical|fatal|crash|panic|abort|timeout|denied|rejected)\b")
JSON_KEEP_KEY_RE = re.compile(r"(?i)^(error|errors|message|msg|stack|stacktrace|stack_trace|trace|traceback|exception|reason|detail|details|warning|warnings)$")


def marker(n: int) -> str:
    return f"… {n} lines removed (shrink) …"


# ---------------------------------------------------------------- terminal cleanup

def clean_line(line: str) -> str:
    if "\r" in line:
        segs = [ANSI_RE.sub("", s) for s in line.split("\r")]
        visible = [s for s in segs if s.strip()]
        line = visible[-1] if visible else ""
    return ANSI_RE.sub("", line)


# ---------------------------------------------------------------- anti-truncation guard

def vocab(line: str) -> tuple[frozenset, bool]:
    toks = [DIGITS_RE.sub("#", t) for t in TOKEN_RE.findall(line.lower())]
    words = [t for t in toks if any(c.isalpha() for c in t)]
    if words:
        return frozenset(toks), True
    return frozenset(TOKEN_RE.findall(line.lower())), False


def represented(v: tuple[frozenset, bool], classes: list[tuple[frozenset, bool]]) -> bool:
    voc, masked = v
    if not voc:
        return True
    need = max(1, int(CONTAINMENT * len(voc) + 0.999999))
    for cv, cm in classes:
        if cm == masked and len(voc & cv) >= need:
            return True
    return False


def keep_non_redundant(units: list[str], keep: list[bool]) -> None:
    """redundancy.go: a unit that is unique of its kind survives at least once."""
    kept = [i for i, k in enumerate(keep) if k]
    step = max(1, len(kept) // MAX_CLASSES) if kept else 1
    classes = [vocab(units[kept[n]]) for n in range(0, len(kept), step)][:MAX_CLASSES]
    promotions = 0
    for i, u in enumerate(units):
        if keep[i]:
            continue
        if promotions >= MAX_PROMOTIONS:
            keep[i] = True          # too many distinct units: do not truncate
            continue
        v = vocab(u)
        if represented(v, classes):
            continue
        keep[i] = True
        classes.append(v)
        promotions += 1


# ---------------------------------------------------------------- text (build, tests, logs, YAML)

def must_keep(lines: list[str]) -> list[bool]:
    """Lines to keep: failed tests, messages, useful stack frames, diagnostics, summary."""
    keep = [False] * len(lines)
    in_msg = False
    frames_left = 0
    for i, ln in enumerate(lines):
        if MSG_START_RE.match(ln):
            keep[i] = True
            in_msg = True
            continue
        if STACK_START_RE.match(ln):
            keep[i] = True
            in_msg = False
            frames_left = STACK_FRAMES
            continue
        if DIAG_RE.search(ln) or SUMMARY_RE.search(ln) or FAILED_TEST_RE.match(ln):
            keep[i] = True
            in_msg = False
            if FAILED_TEST_RE.match(ln):
                frames_left = STACK_FRAMES
            continue
        if SECTION_END_RE.match(ln):
            in_msg = False
            continue
        if in_msg:                       # multi-line error message: kept whole
            keep[i] = True
            continue
        if FRAME_RE.match(ln):
            if USEFUL_FRAME_RE.search(ln) and frames_left > 0:
                keep[i] = True
                frames_left -= 1
            continue
        if IMPORTANT_RE.search(ln):
            keep[i] = True
    return keep


def dedupe_important(lines: list[str], keep: list[bool]) -> int:
    """MSBuild repeats every diagnostic in its summary: one exact copy is enough."""
    seen: set[str] = set()
    removed = 0
    for i, ln in enumerate(lines):
        if keep[i] and DIAG_RE.search(ln):
            key = ln.strip()
            if key in seen:
                keep[i] = False
                removed += 1
            else:
                seen.add(key)
    return removed


def collapse_repeats(lines: list[str]) -> list[str]:
    """repetition.go: 3 or more identical consecutive lines become one line plus a counter."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[i]:
            j += 1
        run = j - i + 1
        out.append(lines[i])
        if run >= REP_MIN_RUN and lines[i].strip():
            out.append(f"… ×{run} (shrink) …")
        else:
            out.extend(lines[i + 1:j + 1])
        i = j + 1
    return out


def shrink_text(text: str) -> str:
    lines = [clean_line(l) for l in text.split("\n")]
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    lines = collapse_repeats(lines)
    if len(lines) < MIN_LINES:
        return "\n".join(lines) + ("\n" if trailing else "")
    keep = must_keep(lines)
    n = len(lines)
    for i in list(range(min(KEEP_HEAD, n))) + list(range(max(0, n - KEEP_TAIL), n)):
        keep[i] = True
    dup = dedupe_important(lines, keep)
    blank_drop = [not keep[i] and not lines[i].strip() for i in range(n)]
    keep_non_redundant(lines, keep)
    out: list[str] = []
    dropped = 0
    for i, ln in enumerate(lines):
        if keep[i]:
            if dropped:
                out.append(marker(dropped))
                dropped = 0
            out.append(ln)
        elif not blank_drop[i]:
            dropped += 1
    if dropped:
        out.append(marker(dropped))
    if dup:
        out.append(f"… {dup} duplicate diagnostics removed (shrink) …")
    return "\n".join(out) + ("\n" if trailing else "")


# ---------------------------------------------------------------- JSON

def _shape(e) -> str:
    if isinstance(e, dict):
        return "{" + ",".join(sorted(e)) + "}"
    return type(e).__name__


def shrink_json_value(v, key: str = ""):
    if key and JSON_KEEP_KEY_RE.match(key):
        return v                                    # error/message/stack...: kept whole
    if isinstance(v, dict):
        return {k: shrink_json_value(x, k) for k, x in v.items()}
    if not isinstance(v, list):
        return v
    items = [shrink_json_value(x) for x in v]
    n = len(items)
    if n < ARRAY_MIN:
        return items
    docs = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in v]
    keep = [i < ARRAY_FIRST or i >= n - ARRAY_LAST or bool(JSON_ERROR_RE.search(docs[i])) for i in range(n)]
    shapes = {_shape(v[i]) for i in range(n) if keep[i]}
    for i in range(n):                              # new element shape: kept once
        if not keep[i] and _shape(v[i]) not in shapes:
            keep[i] = True
            shapes.add(_shape(v[i]))
    out, run = [], 0
    for i in range(n):
        if keep[i]:
            if run:
                out.append({"__shrink_elided__": run})
                run = 0
            out.append(items[i])
        else:
            run += 1
    if run:
        out.append({"__shrink_elided__": run})
    return out


def shrink_json(text: str):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, (dict, list)):
        return None
    return json.dumps(shrink_json_value(data), ensure_ascii=False, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------- CSV / TSV

def shrink_table(text: str):
    lines = text.rstrip("\n").split("\n")
    if len(lines) < ARRAY_MIN + 1:
        return None
    for sep in ("\t", ",", ";"):
        cols = lines[0].count(sep)
        if cols >= 1 and all(l.count(sep) == cols for l in lines[1:]):
            break
    else:
        return None
    rows = lines[1:]
    n = len(rows)
    keep = [i < ARRAY_FIRST or i >= n - ARRAY_LAST or bool(JSON_ERROR_RE.search(r)) for i, r in enumerate(rows)]
    out, run = [lines[0]], 0
    for i, r in enumerate(rows):
        if keep[i]:
            if run:
                out.append(marker(run))
                run = 0
            out.append(r)
        else:
            run += 1
    if run:
        out.append(marker(run))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- test reports (.trx, JUnit)

def _trim_stack(stack: str) -> list[str]:
    frames = [l.rstrip() for l in stack.splitlines() if l.strip()]
    useful = [f for f in frames if USEFUL_FRAME_RE.search(f)]
    frames = useful or frames
    if len(frames) >= STACK_HEAD + STACK_TAIL + 3:
        frames = frames[:STACK_HEAD] + [marker(len(frames) - STACK_HEAD - STACK_TAIL)] + frames[-STACK_TAIL:]
    return frames


def shrink_test_report(text: str):
    head = text.lstrip()[:400]
    if not head.startswith("<") or ("<TestRun" not in head and "<testsuite" not in head):
        return None
    try:
        root = ET.fromstring(text.lstrip().encode("utf-8"))
    except ET.ParseError:
        return None
    tag = lambda e: e.tag.rsplit("}", 1)[-1]
    out: list[str] = []
    if tag(root) == "TestRun":
        counters = next((e for e in root.iter() if tag(e) == "Counters"), None)
        if counters is not None:
            c = counters.attrib
            out.append(f"Total {c.get('total')}: passed {c.get('passed')}, failed {c.get('failed')}, "
                       f"not executed {c.get('notExecuted')}, errors {c.get('error')}")
        for r in root.iter():
            if tag(r) != "UnitTestResult" or r.get("outcome") not in ("Failed", "Error", "Timeout", "Aborted"):
                continue
            out.append(f"  Failed {r.get('testName')} [{r.get('duration')}]")
            msg = next((e.text or "" for e in r.iter() if tag(e) == "Message"), "")
            st = next((e.text or "" for e in r.iter() if tag(e) == "StackTrace"), "")
            out.append("  Error Message:")
            out.extend("   " + l for l in msg.strip().splitlines())
            if st.strip():
                out.append("  Stack Trace:")
                out.extend("  " + l for l in _trim_stack(st))
    else:
        cases = [e for e in root.iter() if tag(e) == "testcase"]
        failed = 0
        body: list[str] = []
        for tc in cases:
            f = next((e for e in tc if tag(e) in ("failure", "error")), None)
            if f is None:
                continue
            failed += 1
            body.append(f"  Failed {tc.get('classname', '')}.{tc.get('name')}")
            body.append("  Error Message:")
            body.append("   " + (f.get("message") or "").strip())
            body.extend("  " + l for l in _trim_stack(f.text or ""))
        out.append(f"Total {len(cases)}: failed {failed}")
        out.extend(body)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- entry point

def shrink(text: str) -> str:
    text = text.lstrip("\ufeff")
    if len(text) < MIN_CHARS:
        return "\n".join(clean_line(l) for l in text.split("\n"))
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        r = shrink_json(text)
        if r is not None:
            return r if len(r) < len(text) else text
    if stripped[:1] == "<":
        r = shrink_test_report(text)
        if r is not None:
            return r
    r = shrink_table(text)
    if r is not None and len(r) < len(text):
        return r
    r = shrink_text(text)
    return r if len(r) < len(text) else text


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        if len(argv) < 2:
            print("usage: shrink.py -- <command...>", file=sys.stderr)
            return 2
        try:
            p = subprocess.run(argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            print(f"shrink.py: command not found: {argv[1]}", file=sys.stderr)
            return 127
        sys.stdout.write(shrink(p.stdout.decode("utf-8", "replace")))
        sys.stdout.flush()
        return p.returncode if p.returncode >= 0 else 128 - p.returncode
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv:
        with open(argv[0], encoding="utf-8", errors="replace") as f:
            data = f.read()
    else:
        data = sys.stdin.buffer.read().decode("utf-8", "replace")
    sys.stdout.write(shrink(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
