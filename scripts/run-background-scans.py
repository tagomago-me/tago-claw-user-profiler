#!/usr/bin/env python3
"""
Run user-profiler background scans.

This runner:
- loads required API keys from /data/.openclaw/openclaw.json when missing in env
- executes scans in a fixed order
- relies on each scan script's own interval gating (too_soon logic)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path("/data")
OPENCLAW_CONFIG = WORKSPACE / ".openclaw" / "openclaw.json"
PROFILER_DIR = WORKSPACE / "skills" / "user-profiler" / "scripts"

SCANS = [
    "scan-nostr.py",
    "scan-notion.py",
    "scan-blog.py",
]

ENV_KEYS = [
    "OPENAI_API_KEY",
    "NOTION_API_KEY",
    "NOSTR_DAMUS_PUBLIC_HEX_KEY",
    "NOSTR_DAMUS_PRIVATE_HEX_KEY",
]
DEFAULT_REVIEW_TARGET = "-5143886918"


def _extract_key(node: object, wanted: str) -> str | None:
    if isinstance(node, dict):
        direct = node.get(wanted)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        if node.get("name") == wanted:
            v = node.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()

        for v in node.values():
            found = _extract_key(v, wanted)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _extract_key(item, wanted)
            if found:
                return found
    return None


def load_config_env() -> None:
    if not OPENCLAW_CONFIG.exists():
        return
    try:
        obj = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return

    for key in ENV_KEYS:
        if os.environ.get(key):
            continue
        value = _extract_key(obj, key)
        if value:
            os.environ[key] = value


def run_scan(script_name: str) -> int:
    script = PROFILER_DIR / script_name
    if not script.exists():
        print(f"[missing] {script}")
        return 1

    print(f"[start] {script_name}")
    proc = subprocess.run(["python3", str(script)], text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    print(f"[done] {script_name} rc={proc.returncode}")
    return proc.returncode


def send_review_cards() -> int:
    target = os.environ.get("USER_PROFILER_REVIEW_TARGET", "").strip() or DEFAULT_REVIEW_TARGET
    script = PROFILER_DIR / "send-telegram-review-batch.py"
    if not script.exists():
        print(f"[missing] {script}")
        return 1

    print(f"[start] {script.name} target={target}")
    proc = subprocess.run(
        ["python3", str(script), "--target", target, "--limit", "4", "--silent"],
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    print(f"[done] {script.name} rc={proc.returncode}")
    return proc.returncode


def main() -> int:
    load_config_env()
    rc_total = 0
    for script_name in SCANS:
        rc = run_scan(script_name)
        if rc != 0:
            rc_total = 1
    rc = send_review_cards()
    if rc != 0:
        rc_total = 1
    return rc_total


if __name__ == "__main__":
    raise SystemExit(main())
