#!/usr/bin/env python3
"""User-profiler Gmail scan (Mauro).

Goal: build a cheap, structured index of Mauro's recent email episodes for later
selection + evidence extraction, without changing the existing user-profiler
cron behaviour.

- Does NOT write to USER.md.
- Output is a JSONL index under /data/memory/.

This is deliberately conservative (low token/cost): we avoid fetching full
bodies by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", re.I)

DEFAULT_OUT = Path("/data/memory/user-profiler-gmail-index.jsonl")


def sh(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True)


def gog_json(cmd: list[str]) -> Any:
    return json.loads(sh(cmd))


def classify_subject(subject: str) -> str:
    s = (subject or "").strip().lower()
    if not s:
        return "unknown"
    noisy_prefixes = (
        "aceita:",
        "aceito:",
        "accepted:",
        "declined:",
        "convite",
        "invitation",
        "compromisso agendado",
        "calendar",
    )
    if any(s.startswith(p) for p in noisy_prefixes):
        return "calendar"
    if "meeting report" in s or "read meeting report" in s:
        return "auto"
    return "normal"


def should_scan_for_profile(subject: str) -> tuple[bool, str]:
    s = (subject or "").strip().lower()
    if classify_subject(subject) in {"calendar", "auto"}:
        # Allow rare exceptions by keyword, otherwise NO.
        key = ("icms", "btt", "biotechtown", "codemge", "finep", "eisenhower")
        if any(k in s for k in key):
            return True, "calendar/auto but contains high-signal keyword"
        return False, "calendar/auto"

    high_signal = (
        "proposta",
        "contrapartida",
        "finep",
        "subvenção",
        "subvencao",
        "trl",
        "earn-out",
        "due diligence",
        "aquisição",
        "aquisicao",
        "termo",
        "negociação",
        "negociacao",
        "codemge",
        "biotechtown",
        "btt",
        "eisenhower",
    )
    if any(k in s for k in high_signal):
        return True, "high-signal keyword"
    return False, "no obvious signal"


def score_thread(subject: str, message_count: int | None) -> tuple[float, str, list[str]]:
    """Cheap scoring for prioritization.

    Returns: (score_0_10, bucket, reasons)
    """
    s = (subject or "").strip().lower()
    n = int(message_count or 0)
    score = 0.0
    reasons: list[str] = []

    kind = classify_subject(subject)
    if kind == "calendar":
        score -= 4.0
        reasons.append("calendar")
    elif kind == "auto":
        score -= 3.0
        reasons.append("auto")

    # conversational density proxy
    if n >= 8:
        score += 2.0
        reasons.append(f"thread_msgs={n}")
    elif n >= 4:
        score += 1.0
        reasons.append(f"thread_msgs={n}")

    weights = {
        "proposta": 2.0,
        "contrapartida": 2.0,
        "finep": 2.0,
        "subvenção": 2.0,
        "subvencao": 2.0,
        "trl": 2.0,
        "earn-out": 2.5,
        "due diligence": 2.5,
        "aquisição": 2.5,
        "aquisicao": 2.5,
        "termo": 1.5,
        "negociação": 2.0,
        "negociacao": 2.0,
        "codemge": 2.0,
        "biotechtown": 2.5,
        "btt": 2.0,
        "icms": 2.0,
        "eisenhower": 2.0,
        "investimento": 2.0,
        "captação": 2.0,
        "captacao": 2.0,
    }
    for k, w in weights.items():
        if k in s:
            score += w
            reasons.append(k)

    # clamp
    score = max(0.0, min(10.0, score))
    bucket = "not"
    if score >= 7.0:
        bucket = "must"
    elif score >= 4.0:
        bucket = "nice"
    return score, bucket, reasons[:8]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("GOG_ACCOUNT", "mauro.rebelo@biobureau.com.br"))
    ap.add_argument("--self-email", default=os.environ.get("GOG_SELF_EMAIL", "mauro.rebelo@biobureau.com.br"), help="Email identity to treat as 'self'")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--only-from-self", dest="only_from_self", action="store_true", help="Index only threads where From is self")
    g.add_argument("--include-to-self", dest="only_from_self", action="store_false", help="Also include inbound threads (to self)")
    ap.set_defaults(only_from_self=True)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    self_email = (args.self_email or "").strip()
    q = f"newer_than:{int(args.days)}d"
    if args.only_from_self:
        q = f"{q} from:{self_email}"
    else:
        q = f"{q} (from:{self_email} OR to:{self_email})"
    threads = gog_json([
        "gog",
        "gmail",
        "search",
        q,
        "--json",
        "--limit",
        str(int(args.limit)),
        "--account",
        args.account,
    ]).get("threads", [])

    now = datetime.now(timezone.utc).isoformat()
    counts = Counter()

    # We index per thread (episode grouping can be refined later).
    with out_path.open("w", encoding="utf-8") as f:
        for t in threads:
            tid = str(t.get("id") or "").strip()
            subject = str(t.get("subject") or "").strip()
            kind = classify_subject(subject)
            scan, reason = should_scan_for_profile(subject)
            density_score, bucket_suggested, score_reasons = score_thread(subject, t.get("messageCount"))

            rec = {
                "schema": "user_profiler.gmail.index.v1",
                "generated_at": now,
                "account": args.account,
                "thread_id": tid,
                "date": t.get("date"),
                "from": t.get("from"),
                "subject": subject,
                "message_count": t.get("messageCount"),
                "labels": t.get("labels"),
                "kind": kind,
                "scan_for_profile": bool(scan),
                "scan_reason": reason,
                "density_score": density_score,
                "bucket_suggested": bucket_suggested,
                "score_reasons": score_reasons,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            counts["threads_total"] += 1
            counts[f"kind_{kind}"] += 1
            counts["scan_yes"] += 1 if scan else 0
            counts["scan_no"] += 0 if scan else 1

    print(json.dumps({"ok": True, "out": str(out_path), "counts": dict(counts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
