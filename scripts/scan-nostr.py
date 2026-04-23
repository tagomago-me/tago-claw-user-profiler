#!/usr/bin/env python3
"""
Scan recent Nostr posts from the local DuckDB mirror and extract candidate
profile traits into DuckDB.

This stage does NOT decide reinforcement/contradiction against USER.md.
It only proposes behavioral traits from individual notes and links each note
to the proposed trait in the database.
"""

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime

import duckdb

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

STATE_PATH = '/data/memory/profiler-state.json'
DUCKDB_PATH = '/data/userprofile/userprofile.duckdb'
SCHEMA_PATH = '/data/userprofile/sql/schema.sql'
MIN_INTERVAL = 3
ANALYSIS_VERSION = '2026-04-22-traits-b'
POST_LIMIT = 12
QUERY_SCAN_LIMIT = 200
RAW_SOURCE_PATH = '/data/skills/user-profiler/scripts/scan-nostr.py'
BOOTSTRAP_TRAIT_THRESHOLD = int(os.environ.get('USER_PROFILER_BOOTSTRAP_THRESHOLD', '20'))
TRAIT_SHORTLIST_LIMIT = int(os.environ.get('USER_PROFILER_TRAIT_SHORTLIST_LIMIT', '12'))

GENERIC_PREFIXES = (
    'mauro is interested in',
    'mauro has an interest in',
    'mauro is intrigued by',
    'mauro is involved in',
    'mauro engages with',
    'mauro is attentive to',
    'mauro has a background in',
    'mauro appreciates',
    'mauro values',
    'mauro expresses a preference for',
)


def load_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding='utf-8') as f:
            state = json.load(f)
    state.setdefault('evidence', {})
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    try:
        subprocess.run(['python3', '/data/userprofile/scripts/sync_profiler_state.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        pass


def too_soon(state, key='lastNostrScan'):
    if os.environ.get('USER_PROFILER_FORCE_DAILY', '').strip() in {'1', 'true', 'yes', 'on'}:
        return False
    last = state.get(key)
    return bool(last) and (time.time() - last) / 86400 < MIN_INTERVAL


def get_pubkey():
    return os.environ.get('NOSTR_DAMUS_PUBLIC_HEX_KEY', '').strip()


def looks_like_operational_noise(content):
    text = (content or '').strip().lower()
    if not text:
        return True
    noisy_markers = [
        'e2e nip96', 'nip96 final', 'nip96 perm', 'nip96 media test',
        'fix test', 'final check', 'final pass', 'perm fix',
    ]
    if any(marker in text for marker in noisy_markers):
        return True
    if len(text) < 20:
        return True
    if text.count('http') >= 3 and len(text.split()) < 12:
        return True
    return False


def fetch_notes(pubkey, count=POST_LIMIT, before_ts=None, scan_limit=QUERY_SCAN_LIMIT):
    q = """
    select event_id, event_created_at, content
    from mart.nostr_events_semantic
    where pubkey = ?
      and event_type = 'post'
      and coalesce(trim(content), '') <> ''
      and (? is null or event_created_at < to_timestamp(?))
    order by event_created_at desc
    limit ?
    """
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    rows = con.execute(q, [pubkey, before_ts, before_ts, scan_limit]).fetchall()
    events = []
    for event_id, created_at, content in rows:
        if looks_like_operational_noise(content):
            continue
        created_ts = int(created_at.timestamp()) if hasattr(created_at, 'timestamp') else 0
        events.append({'id': event_id, 'created_at': created_ts, 'content': content})
        if len(events) >= count:
            break
    return events


def ensure_evidence_record(state, evidence_id, payload):
    bucket = state.setdefault('evidence', {})
    rec = bucket.get(evidence_id, {})
    rec.update({k: v for k, v in payload.items() if v is not None})
    bucket[evidence_id] = rec
    return rec


def note_has_resolved_trait_outcome(event_id):
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    row = con.execute(
        """
        select count(*)
        from raw.profile_trait_note_links l
        join raw.profile_traits t on t.trait_id = l.trait_id
        where l.event_id = ?
          and coalesce(t.status, 'proposed') in ('validated', 'proposed')
        """,
        [event_id],
    ).fetchone()
    return bool(row and row[0] > 0)


def already_analyzed(state, evidence_id):
    rec = state.get('evidence', {}).get(evidence_id, {})
    if not (rec.get('analyzed_at') and rec.get('analysis_version') == ANALYSIS_VERSION):
        return False
    event_id = (evidence_id or '').split('nostr:', 1)[-1]
    if not event_id or event_id == evidence_id:
        return False
    return note_has_resolved_trait_outcome(event_id)


def mark_analyzed(state, evidence_ids):
    now = time.time()
    for evidence_id in evidence_ids:
        rec = state.setdefault('evidence', {}).setdefault(evidence_id, {})
        rec['analyzed_at'] = now
        rec['analysis_version'] = ANALYSIS_VERSION


def call_openai(prompt):
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        print('ERROR: OPENAI_API_KEY not set', file=sys.stderr)
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps({
            'model': 'gpt-4o',
            'max_tokens': 300,
            'temperature': 0.2,
            'messages': [{'role': 'user', 'content': prompt}],
        }).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
    )
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'OpenAI error: {e}', file=sys.stderr)
        return None


def parse_json_response(raw):
    raw = raw.strip()
    if raw.startswith('```'):
        raw = '\n'.join(raw.split('\n')[1:])
    if raw.endswith('```'):
        raw = raw[:-3]
    try:
        return json.loads(raw.strip())
    except Exception as e:
        print(f'JSON parse error: {e}\nRaw: {raw[:300]}', file=sys.stderr)
        return None


def trait_allowed(text):
    t = (text or '').strip()
    tl = t.lower()
    if not t:
        return False
    if len(t.split()) < 6:
        return False
    if any(tl.startswith(prefix) for prefix in GENERIC_PREFIXES):
        return False
    return True


def normalize_trait_text(text):
    text = (text or '').strip()
    text = ' '.join(text.split())
    return text


def trait_id_for(text):
    normalized = normalize_trait_text(text).lower()
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:16]


def link_id_for(trait_id, event_id, relation_type):
    return hashlib.sha1(f'{trait_id}|{event_id}|{relation_type}'.encode('utf-8')).hexdigest()[:20]


def current_mode(con):
    count = con.execute("select count(*) from raw.profile_traits where coalesce(status, 'proposed') = 'validated'").fetchone()[0]
    return ('bootstrap' if count < BOOTSTRAP_TRAIT_THRESHOLD else 'matching', count)


def shortlist_traits(con, note, limit=TRAIT_SHORTLIST_LIMIT):
    content = (note.get('content') or '').strip().lower()
    words = [w.strip('.,:;!?()[]{}"\'') for w in content.split() if len(w.strip('.,:;!?()[]{}"\'')) >= 5]
    words = [w for w in words if w]
    if not words:
        return con.execute(
            "select trait_id, trait_text, proposal_count, coalesce(human_bucket,'') from raw.profile_traits where coalesce(status, 'proposed') = 'validated' order by proposal_count desc, last_seen_at desc limit ?",
            [limit],
        ).fetchall()
    clauses = []
    params = []
    for w in words[:8]:
        clauses.append("lower(trait_text) like ?")
        params.append(f'%{w}%')
    where = ' OR '.join(clauses) if clauses else 'TRUE'
    q = f"""
    select trait_id, trait_text, proposal_count, coalesce(human_bucket,'') as human_bucket
    from raw.profile_traits
    where coalesce(status, 'proposed') = 'validated'
      and ({where})
    order by proposal_count desc, last_seen_at desc
    limit ?
    """
    params.append(limit)
    rows = con.execute(q, params).fetchall()
    if rows:
        return rows
    return con.execute(
        "select trait_id, trait_text, proposal_count, coalesce(human_bucket,'') from raw.profile_traits where coalesce(status, 'proposed') = 'validated' order by proposal_count desc, last_seen_at desc limit ?",
        [limit],
    ).fetchall()


def extract_traits_for_note(note, mode='bootstrap', trait_shortlist=None):
    created = datetime.fromtimestamp(note.get('created_at', 0)).strftime('%Y-%m-%d')
    content = (note.get('content') or '').strip()
    if mode == 'matching' and trait_shortlist:
        shortlist_text = '\n'.join(
            f'- {trait_id}: {trait_text} (count={proposal_count}, human={human_bucket or "unrated"})'
            for trait_id, trait_text, proposal_count, human_bucket in trait_shortlist
        )
        prompt = f"""You are matching a SINGLE Nostr note against an existing library of user-profile traits.

Pick an existing trait when it already covers the note well enough.
Only propose a new trait if none of the existing ones fit.
Do not output generic topic interests.

NOTE:
EVIDENCE_ID: nostr:{note.get('id')}
DATE: {created}
TEXT: {content}

EXISTING TRAIT SHORTLIST:
{shortlist_text}

Rules:
- Prefer reusing an existing trait_id.
- At most 2 decisions total.
- A new trait is allowed only when the shortlist genuinely misses the behavior/framing signal.
- Good traits describe a framing habit, preference, recurring stance, tradeoff, friction point, or way of acting.
- Bad traits are topic-interest summaries.
- Keep evidence_excerpt literal, short, and copied from the note.
- confidence must be low|medium|high.

Return ONLY valid JSON:
{{"decisions":[
  {{"action":"use_existing","trait_id":"...","evidence_excerpt":"...","confidence":"medium"}},
  {{"action":"propose_new","text":"...","evidence_excerpt":"...","confidence":"medium"}}
]}}
"""
    else:
        prompt = f"""You are extracting candidate user-profile traits from a SINGLE Nostr note.

Return only behavioral/framing traits that are actually suggested by this note.
Do not decide whether the trait is already in the profile. Do not look for reinforcement.
Do not output generic topic interests.

NOTE:
EVIDENCE_ID: nostr:{note.get('id')}
DATE: {created}
TEXT: {content}

Rules:
- At most 1 new trait.
- Good: describes a framing habit, preference, recurring stance, tradeoff, friction point, or way of acting.
- Bad: subject interest summaries like "Mauro is interested in technology".
- If this note has no real profile signal, return an empty list.
- Keep evidence_excerpt literal, short, and copied from the note.
- confidence must be low|medium|high.

Return ONLY valid JSON:
{{"traits":[{{"text":"...","evidence_excerpt":"...","confidence":"medium"}}]}}
"""
    raw = call_openai(prompt)
    if not raw:
        return []
    parsed = parse_json_response(raw)
    if not parsed:
        return []
    out = []
    if mode == 'matching' and 'decisions' in parsed:
        for item in parsed.get('decisions', []):
            action = (item.get('action') or '').strip()
            excerpt = (item.get('evidence_excerpt') or '').strip()
            confidence = (item.get('confidence') or 'medium').strip().lower()
            if confidence not in {'low', 'medium', 'high'}:
                confidence = 'medium'
            if action == 'use_existing' and item.get('trait_id'):
                out.append({'action': 'use_existing', 'trait_id': item.get('trait_id'), 'evidence_excerpt': excerpt[:500], 'confidence': confidence})
            elif action == 'propose_new':
                text = normalize_trait_text(item.get('text', ''))
                if trait_allowed(text):
                    out.append({'action': 'propose_new', 'text': text, 'evidence_excerpt': excerpt[:500], 'confidence': confidence})
        return out[:2]
    for item in parsed.get('traits', []):
        text = normalize_trait_text(item.get('text', ''))
        excerpt = (item.get('evidence_excerpt') or '').strip()
        confidence = (item.get('confidence') or 'medium').strip().lower()
        if not trait_allowed(text):
            continue
        if confidence not in {'low', 'medium', 'high'}:
            confidence = 'medium'
        out.append({'action': 'propose_new', 'text': text, 'evidence_excerpt': excerpt[:500], 'confidence': confidence})
    return out[:1]


def ensure_schema(con):
    with open(SCHEMA_PATH, encoding='utf-8') as f:
        con.execute(f.read())


def upsert_trait(con, note, trait):
    trait_id = trait_id_for(trait['text'])
    created_at = datetime.fromtimestamp(note.get('created_at', 0))
    trait_raw = json.dumps({
        'trait_id': trait_id,
        'trait_text': trait['text'],
        'origin_source': 'nostr',
        'event_id': note.get('id'),
        'created_at': created_at.isoformat(),
    }, ensure_ascii=False)
    con.execute(
        """
        INSERT INTO raw.profile_traits (
          trait_id, trait_text, status, origin_source, first_seen_at, last_seen_at,
          proposal_count, raw_json, raw_sha256, raw_source_path
        ) VALUES (?, ?, 'proposed', 'nostr', ?, ?, 1, ?, ?, ?)
        ON CONFLICT (trait_id) DO UPDATE SET
          trait_text=excluded.trait_text,
          last_seen_at=excluded.last_seen_at,
          proposal_count=coalesce(raw.profile_traits.proposal_count, 0) + 1,
          raw_json=excluded.raw_json,
          raw_sha256=excluded.raw_sha256,
          imported_at=now(),
          raw_source_path=excluded.raw_source_path
        """,
        [trait_id, trait['text'], created_at, created_at, trait_raw, hashlib.sha256(trait_raw.encode('utf-8')).hexdigest(), RAW_SOURCE_PATH],
    )
    return trait_id


def upsert_link(con, note, trait_id, relation_type, evidence_excerpt, confidence):
    created_at = datetime.fromtimestamp(note.get('created_at', 0))
    link_id = link_id_for(trait_id, note.get('id'), relation_type)
    link_raw = json.dumps({
        'link_id': link_id,
        'trait_id': trait_id,
        'event_id': note.get('id'),
        'relation_type': relation_type,
        'evidence_excerpt': evidence_excerpt or '',
        'llm_confidence': confidence or 'medium',
    }, ensure_ascii=False)
    con.execute(
        """
        INSERT INTO raw.profile_trait_note_links (
          link_id, trait_id, event_id, relation_type, evidence_excerpt,
          llm_confidence, validated_by_human, created_at, raw_json, raw_sha256, raw_source_path
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        ON CONFLICT (link_id) DO UPDATE SET
          evidence_excerpt=excluded.evidence_excerpt,
          llm_confidence=excluded.llm_confidence,
          raw_json=excluded.raw_json,
          raw_sha256=excluded.raw_sha256,
          imported_at=now(),
          raw_source_path=excluded.raw_source_path
        """,
        [link_id, trait_id, note.get('id'), relation_type, evidence_excerpt or '', confidence or 'medium', created_at, link_raw, hashlib.sha256(link_raw.encode('utf-8')).hexdigest(), RAW_SOURCE_PATH],
    )
    return link_id


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true', help='scan older notes instead of the most recent window')
    ap.add_argument('--before-ts', type=int, default=None, help='only consider notes older than this unix timestamp')
    ap.add_argument('--count', type=int, default=POST_LIMIT, help='number of notes to analyze in this run')
    ap.add_argument('--scan-limit', type=int, default=QUERY_SCAN_LIMIT, help='raw candidate notes to inspect before filtering noise')
    ap.add_argument('--headless-ok', action='store_true', help='allow backfill without immediate human card review')
    return ap.parse_args()


def main():
    args = parse_args()
    if args.backfill and not args.headless_ok:
        print('ERROR: backfill requires immediate human card review; refuse headless run without --headless-ok', file=sys.stderr)
        sys.exit(2)
    state = load_state()
    if not args.backfill and too_soon(state, 'lastNostrScan'):
        last = datetime.fromtimestamp(state['lastNostrScan']).strftime('%Y-%m-%d')
        print(f'Skipping: last Nostr scan was {last} (min interval: {MIN_INTERVAL}d)')
        return

    pubkey = get_pubkey()
    if not pubkey:
        print('ERROR: Nostr public key not found', file=sys.stderr)
        sys.exit(1)

    before_ts = args.before_ts
    if args.backfill and before_ts is None:
        cursor = state.get('nostrBackfillBeforeTs')
        if cursor:
            before_ts = int(cursor)
    notes = fetch_notes(pubkey, count=args.count, before_ts=before_ts, scan_limit=args.scan_limit)
    if not notes:
        print('No Nostr notes fetched from DuckDB.')
        return

    pending_notes = []
    pending_ids = []
    for note in notes:
        evidence_id = f"nostr:{note.get('id','')}"
        content = (note.get('content') or '').strip()
        ensure_evidence_record(state, evidence_id, {
            'source': 'nostr',
            'source_id': note.get('id'),
            'created_at': note.get('created_at'),
            'content_hash': hashlib.sha1(content.encode('utf-8')).hexdigest() if content else None,
            'preview': content[:280],
            'quote': content[:500],
            'content_excerpt': content[:500],
        })
        if already_analyzed(state, evidence_id):
            continue
        note['_evidence_id'] = evidence_id
        pending_notes.append(note)
        pending_ids.append(evidence_id)

    print(f'Fetched {len(notes)} Nostr notes from DuckDB, {len(pending_notes)} new to analyze')
    if not pending_notes:
        if args.backfill and notes:
            state['nostrBackfillBeforeTs'] = min(n['created_at'] for n in notes)
        else:
            state['lastNostrScan'] = time.time()
        save_state(state)
        print('No unanalyzed Nostr notes.')
        return

    con = duckdb.connect(DUCKDB_PATH)
    ensure_schema(con)

    mode, trait_library_count = current_mode(con)
    trait_count = 0
    link_count = 0
    reused_count = 0
    for note in pending_notes:
        shortlist = shortlist_traits(con, note) if mode == 'matching' else []
        decisions = extract_traits_for_note(note, mode=mode, trait_shortlist=shortlist)
        produced_for_note = False
        for decision in decisions:
            if decision.get('action') == 'use_existing':
                trait_id = decision.get('trait_id')
                if trait_id:
                    upsert_link(con, note, trait_id, 'origin', decision.get('evidence_excerpt', ''), decision.get('confidence', 'medium'))
                    link_count += 1
                    reused_count += 1
                    produced_for_note = True
            else:
                trait_id = upsert_trait(con, note, decision)
                upsert_link(con, note, trait_id, 'origin', decision.get('evidence_excerpt', ''), decision.get('confidence', 'medium'))
                trait_count += 1
                link_count += 1
                produced_for_note = True
        if not produced_for_note:
            rec = state.setdefault('evidence', {}).setdefault(note['_evidence_id'], {})
            rec['trait_scan_no_signal_at'] = time.time()
            rec['trait_scan_no_signal_version'] = ANALYSIS_VERSION

    mark_analyzed(state, pending_ids)
    if args.backfill:
        state['nostrBackfillBeforeTs'] = min(n['created_at'] for n in notes)
    else:
        state['lastNostrScan'] = time.time()
    save_state(state)

    ready = con.execute("select count(*) from mart.profile_traits_ready_for_review where status = 'proposed'").fetchone()[0]
    if args.backfill:
        print(f'Done: mode={mode}, library={trait_library_count}, {trait_count} new trait proposals, {reused_count} reused traits, {link_count} note links, {ready} traits ready in DuckDB, next_before_ts={state.get("nostrBackfillBeforeTs")}')
    else:
        print(f'Done: mode={mode}, library={trait_library_count}, {trait_count} new trait proposals, {reused_count} reused traits, {link_count} note links, {ready} traits ready in DuckDB')


if __name__ == '__main__':
    main()
