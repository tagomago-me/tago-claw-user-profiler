#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import duckdb

DB_PATH = '/data/userprofile/userprofile.duckdb'
SCAN = '/data/skills/user-profiler/scripts/scan-nostr.py'
REVIEW_BATCH = '/data/skills/user-profiler/scripts/trait-review-batch.py'


def pending_count() -> int:
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        return con.execute("select count(*) from raw.profile_traits where coalesce(status, 'proposed') = 'proposed'").fetchone()[0]
    finally:
        con.close()


def newest_pending(limit: int) -> list[dict]:
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        rows = con.execute(
        """
        select t.trait_id, t.trait_text, t.proposal_count, l.event_id,
               coalesce(l.evidence_excerpt, ''), coalesce(l.llm_confidence, 'medium')
        from raw.profile_traits t
        left join raw.profile_trait_note_links l on l.trait_id = t.trait_id
        where coalesce(t.status, 'proposed') = 'proposed'
        order by t.imported_at desc, l.imported_at desc
        limit ?
        """,
        [limit],
        ).fetchall()
        out = []
        seen = set()
        for trait_id, trait_text, proposal_count, event_id, evidence_excerpt, llm_confidence in rows:
            if trait_id in seen:
                continue
            seen.add(trait_id)
            out.append({
                'trait_id': trait_id,
                'trait_text': trait_text,
                'proposal_count': proposal_count,
                'event_id': event_id,
                'evidence_excerpt': evidence_excerpt,
                'llm_confidence': llm_confidence,
            })
        return out
    finally:
        con.close()


def run_scan(batch_notes: int) -> str:
    proc = subprocess.run(
        ['python3', SCAN, '--backfill', '--headless-ok', '--count', str(batch_notes)],
        text=True,
        capture_output=True,
        env={**__import__('os').environ, 'USER_PROFILER_FORCE_DAILY': '1'},
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or 'scan failed')
    return (proc.stdout or '').strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target-cards', type=int, default=5)
    ap.add_argument('--batch-notes', type=int, default=5)
    ap.add_argument('--max-rounds', type=int, default=12)
    args = ap.parse_args()

    rounds = []
    start_pending = pending_count()
    while pending_count() < args.target_cards and len(rounds) < args.max_rounds:
        out = run_scan(args.batch_notes)
        rounds.append(out)
        time.sleep(0.2)

    final_pending = pending_count()
    items = newest_pending(args.target_cards)
    print(json.dumps({
        'ok': True,
        'start_pending': start_pending,
        'final_pending': final_pending,
        'target_cards': args.target_cards,
        'rounds': rounds,
        'items': items,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
