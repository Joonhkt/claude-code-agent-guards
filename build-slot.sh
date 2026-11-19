#!/bin/bash
# build-slot.sh: run a command (build, tests...) only when one of N slots is free, so that many
# parallel agents do not all compile at the same time and exhaust the RAM.
# Usage: build-slot.sh <command...>     e.g. build-slot.sh dotnet build -m:2
# Environment: BUILD_SLOTS (number of slots, default 2), BUILD_SLOT_DIR (lock directory,
#              default ${TMPDIR:-/tmp}/build-slots-$USER), BUILD_SLOT_WAIT (seconds between tries, default 15).
set -u
[ $# -gt 0 ] || { echo "usage: build-slot.sh <command...>" >&2; exit 2; }
command -v flock >/dev/null || { echo "build-slot.sh: flock is required" >&2; exit 127; }
N=${BUILD_SLOTS:-2}
D=${BUILD_SLOT_DIR:-${TMPDIR:-/tmp}/build-slots-${USER:-user}}
WAIT=${BUILD_SLOT_WAIT:-15}
mkdir -p "$D"
while true; do
  for i in $(seq 1 "$N"); do
    exec {fd}>"$D/slot$i"
    if flock -n "$fd"; then
      "$@"; rc=$?
      flock -u "$fd"
      exit $rc
    fi
    exec {fd}>&-
  done
  sleep "$WAIT"
done
