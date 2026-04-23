#!/usr/bin/env bash
# Invoked by OpenClaw cron (Mon+Thu 09:00 America/Sao_Paulo).
# Current operational mode: Nostr-only user-profiler scan from local DuckDB.
set -uo pipefail
export PATH="/data/bin:/usr/local/bin:/usr/bin:${PATH:-}"
cd /data
ec=0
for s in scan-nostr.py; do
  if ! python3 "/data/skills/user-profiler/scripts/$s"; then
    ec=1
  fi
done
exit "$ec"
