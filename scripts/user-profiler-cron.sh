#!/usr/bin/env bash
# Invoked by OpenClaw cron (Mon+Thu 09:00 America/Sao_Paulo). Each Python script
# self-throttles using /data/memory/profiler-state.json.
set -uo pipefail
export PATH="/data/vps-only/bin:/usr/local/bin:/usr/bin:${PATH:-}"
cd /data
ec=0
for s in scan-nostr.py scan-blog.py scan-notion.py; do
  if ! python3 "/data/skills/user-profiler/scripts/$s"; then
    ec=1
  fi
done
exit "$ec"
