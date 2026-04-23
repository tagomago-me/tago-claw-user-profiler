#!/usr/bin/env python3
"""
Manage behavioral profile: candidates, patterns, contradictions in USER.md.

Commands:
  add-candidate '<json>'       Add a behavioral signal candidate
  approve <id>                 Approve a candidate (writes to USER.md at 4+ signals)
  reject <id>                  Remove a candidate
  rate <id> <must|nice|not|must-have|nice-to-have|never-mind>
                               Rate a candidate's relevance (feedback loop)
  rate-callback <payload>      Parse typed callback like profile_rate:candidate:<id>:<bucket>
  rate-callback-json <payload> Parse typed callback and emit JSON with item/message data
  review-register <id> <message-id>
                               Store Telegram message id for auto-delete
  review-message <id>          Return stored Telegram message id for candidate
  review-clear <id>            Remove stored Telegram message id mapping
  list                         Show all pending candidates
  review                       Show review queue with human rating labels
  review-batch [n]             Emit top N candidates with button callback payloads
  reinforcement-review-batch [n]
                               Emit top N reinforcements with typed callback payloads
  dedupe                       Merge obvious duplicate pending candidates
  memory-input <candidate-id>  Emit memory-writer input with linked EVIDENCE_ID lines
  evidence-summary             Show evidence lifecycle counts
  resolve-evidence <id> <status> [--memory-date YYYY-MM-DD]
                               Mark evidence as promoted_to_memory or discarded
  compact-evidence             Remove heavy fields from resolved evidence, keep stub
  add-contradiction '<json>'   Add a contradiction directly to USER.md
  show                         Show current patterns and contradictions from USER.md

JSON for add-candidate:
  {"text": "...", "evidence": "...", "source": "conversation|nostr", "date": "YYYY-MM-DD",
   "evidence_ids": ["..."]}

JSON for add-contradiction:
  {"a": "...", "b": "...", "evidence_a": "...", "evidence_b": "...",
   "same_domain": true|false, "date": "YYYY-MM-DD"}
"""

import sys
import os
import json
import re
import uuid
import subprocess
from collections import defaultdict
from datetime import date, datetime

USERMD_PATH = '/data/USER.md'
STATE_PATH = '/data/memory/profiler-state.json'
FEEDBACK_PATH = '/data/userprofile/raw/profile_scoring_feedback.jsonl'
LEGACY_FEEDBACK_PATH = '/data/memory/profile-scoring-feedback.jsonl'
USERPROFILE_APPEND_SCRIPT = '/data/userprofile/scripts/append_feedback.py'
USERPROFILE_SYNC_SCRIPT = '/data/userprofile/scripts/sync_profiler_state.py'
REVIEW_MESSAGES_PATH = '/data/.openclaw/user-profiler-review-messages.json'
SIGNALS_REQUIRED = 4
VALID_EVIDENCE_STATUS = {'pending', 'analyzed', 'promoted_to_memory', 'discarded'}
HEAVY_EVIDENCE_FIELDS = {'preview', 'text', 'summary', 'raw', 'body', 'transcript'}

GENERIC_PHRASES = [
    'is interested in',
    'has an interest in',
    'is intrigued by',
    'is involved in',
    'engages with',
    'is attentive to',
    'has a background in',
    'appreciates the role of',
    'expresses a preference for',
    'values',
]

# ─── State file ───────────────────────────────────────────────────────────────

def load_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding='utf-8') as f:
            state = json.load(f)
    state.setdefault('candidates', [])
    state.setdefault('evidence', {})
    state.setdefault('reinforcements', [])
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    if os.path.exists(USERPROFILE_SYNC_SCRIPT):
        try:
            subprocess.run(
                ['python3', USERPROFILE_SYNC_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except Exception:
            pass


def append_feedback(rec):
    raw = json.dumps(rec, ensure_ascii=False)

    if os.path.exists(USERPROFILE_APPEND_SCRIPT):
        try:
            subprocess.run(
                ['python3', USERPROFILE_APPEND_SCRIPT, '--json', raw, '--mirror-legacy'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return
        except Exception:
            pass

    os.makedirs(os.path.dirname(FEEDBACK_PATH), exist_ok=True)
    with open(FEEDBACK_PATH, 'a', encoding='utf-8') as f:
        f.write(raw + '\n')

    os.makedirs(os.path.dirname(LEGACY_FEEDBACK_PATH), exist_ok=True)
    with open(LEGACY_FEEDBACK_PATH, 'a', encoding='utf-8') as f:
        f.write(raw + '\n')


def profile_review_mapping_key(item_type, item_id):
    return f'{item_type}:{item_id}'


def load_review_messages():
    os.makedirs(os.path.dirname(REVIEW_MESSAGES_PATH), exist_ok=True)
    if os.path.exists(REVIEW_MESSAGES_PATH):
        with open(REVIEW_MESSAGES_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_review_messages(data):
    os.makedirs(os.path.dirname(REVIEW_MESSAGES_PATH), exist_ok=True)
    with open(REVIEW_MESSAGES_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def recent_feedback_for_candidate(cid, limit=20):
    out = []
    if not os.path.exists(FEEDBACK_PATH):
        return out
    with open(FEEDBACK_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('schema') in {'user_profiler.feedback.v1', 'user_profiler.review_flag.v1'} and obj.get('candidate_id') == cid:
                out.append(obj)
    return out[-limit:]


def _iso_date_tuple(value):
    text = str(value or '').strip()
    if not text:
        return ()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return tuple(int(x) for x in m.groups())
    return ()


def latest_feedback_date_for_candidate(cid):
    feedback = recent_feedback_for_candidate(cid, limit=50)
    dates = [_iso_date_tuple(rec.get('date')) for rec in feedback if rec.get('schema') == 'user_profiler.feedback.v1']
    dates = [d for d in dates if d]
    return max(dates) if dates else ()


def latest_signal_date_for_candidate(candidate):
    dates = [_iso_date_tuple(sig.get('date')) for sig in candidate.get('signals', [])]
    dates = [d for d in dates if d]
    return max(dates) if dates else ()


def has_feedback_for_candidate(cid):
    feedback = recent_feedback_for_candidate(cid, limit=20)
    return any(rec.get('schema') == 'user_profiler.feedback.v1' for rec in feedback)


def candidate_needs_rereview(candidate):
    cid = candidate.get('id', '')
    if not cid:
        return False
    latest_feedback = latest_feedback_date_for_candidate(cid)
    latest_signal = latest_signal_date_for_candidate(candidate)
    return bool(latest_feedback and latest_signal and latest_signal > latest_feedback)


def unrated_candidates(state):
    out = []
    for c in state.get('candidates', []):
        cid = c.get('id', '')
        if not has_feedback_for_candidate(cid) or candidate_needs_rereview(c):
            out.append(c)
    return out


def infer_evidence_status(rec):
    status = (rec.get('status') or '').strip()
    if status in VALID_EVIDENCE_STATUS:
        return status
    if rec.get('memory_written_at'):
        return 'promoted_to_memory'
    if rec.get('discarded_at'):
        return 'discarded'
    if rec.get('analyzed_at'):
        return 'analyzed'
    return 'pending'


def compact_evidence_record(rec):
    compacted = dict(rec)
    for field in HEAVY_EVIDENCE_FIELDS:
        compacted.pop(field, None)
    compacted['compacted_at'] = str(date.today())
    compacted['stub'] = True
    return compacted


def normalize_text(text):
    text = (text or '').strip().lower()
    text = text.replace('’', "'")
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^a-z0-9\s]+', ' ', text)
    tokens = [t for t in text.split() if t]
    stop = {
        'mauro', 'is', 'a', 'an', 'the', 'and', 'or', 'of', 'to', 'in', 'for',
        'with', 'his', 'her', 'their', 'that', 'this', 'these', 'those', 'how',
        'what', 'when', 'where', 'who', 'can', 'also', 'both', 'be', 'by',
        'on', 'its', 'it', 'as', 'over', 'under', 'into', 'from', 'at',
        'particularly', 'related', 'role', 'use', 'useful', 'systems', 'system',
    }
    filtered = [t for t in tokens if t not in stop]
    return ' '.join(filtered)


def evidence_fingerprint(sig):
    evidence = (sig.get('evidence') or '').strip().lower()
    evidence = evidence.replace('’', "'")
    evidence = re.sub(r'\[[^\]]+\]', ' ', evidence)
    evidence = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', evidence)
    evidence = re.sub(r'\s+', ' ', evidence)
    evidence = re.sub(r'[^a-z0-9\s]+', ' ', evidence)
    words = [w for w in evidence.split() if len(w) > 2]
    return ' '.join(words[:16])


def evidence_record_text(rec):
    for key in ('quote', 'highlight_text', 'transcript_excerpt', 'body_excerpt', 'content_excerpt', 'text', 'preview'):
        value = (rec.get(key) or '').strip()
        if value:
            return value
    return ''


def format_signal_evidence(state, source, evidence_ids, fallback=''):
    evidence = state.get('evidence', {})
    parts = []
    for evidence_id in evidence_ids or []:
        rec = evidence.get(evidence_id) or {}
        quote = evidence_record_text(rec)
        if not quote:
            continue
        rec_source = (rec.get('source') or source or 'evidence').strip()
        meta = []
        for key in ('published_at', 'created_at', 'date', 'title', 'episode', 'show'):
            value = rec.get(key)
            if not value:
                continue
            if key == 'created_at' and isinstance(value, (int, float)):
                value = datetime.fromtimestamp(value).strftime('%Y-%m-%d')
            meta.append(str(value))
            if len(meta) >= 2:
                break
        prefix = f'[{rec_source}] '
        if meta:
            prefix += f'({" | ".join(meta)}) '
        parts.append(prefix + quote)
    if parts:
        return '\n\n'.join(parts[:2])
    return fallback.strip()


def evidence_record_fingerprint(rec):
    preview = evidence_record_text(rec).strip().lower()
    preview = preview.replace('’', "'")
    preview = re.sub(r'\s+', ' ', preview)
    preview = re.sub(r'[^a-z0-9\s]+', ' ', preview)
    words = [w for w in preview.split() if len(w) > 2]
    return ' '.join(words[:16])


def jaccard(a, b):
    sa = set((a or '').split())
    sb = set((b or '').split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def classify_candidate(text):
    lowered = (text or '').strip().lower()
    generic = any(phrase in lowered for phrase in GENERIC_PHRASES)
    return {
        'generic': generic,
        'normalized': normalize_text(text),
    }


def candidate_score(candidate):
    signals = candidate.get('signals', [])
    signal_count = len(signals)
    source_diversity = len({(s.get('source') or '').strip().lower() for s in signals if s.get('source')})
    fingerprints = {evidence_fingerprint(s) for s in signals if evidence_fingerprint(s)}
    evidence_diversity = len(fingerprints)
    meta = classify_candidate(candidate.get('text', ''))
    score = signal_count * 100 + source_diversity * 25 + evidence_diversity * 15
    if meta['generic']:
        score -= 20
    return score


def find_duplicate_clusters(candidates):
    clusters = []
    used = set()
    metas = {c['id']: classify_candidate(c.get('text', '')) for c in candidates}
    fingerprints = {
        c['id']: {evidence_fingerprint(sig) for sig in c.get('signals', []) if evidence_fingerprint(sig)}
        for c in candidates
    }

    for candidate in candidates:
        cid = candidate['id']
        if cid in used:
            continue
        cluster = [candidate]
        used.add(cid)
        for other in candidates:
            oid = other['id']
            if oid in used or oid == cid:
                continue
            same_evidence = bool(fingerprints[cid] & fingerprints[oid])
            similar_text = jaccard(metas[cid]['normalized'], metas[oid]['normalized']) >= 0.45
            if same_evidence or similar_text:
                cluster.append(other)
                used.add(oid)
        clusters.append(cluster)
    return clusters


def group_by_quality(candidates):
    strong, weak = [], []
    for c in candidates:
        signals = len(c.get('signals', []))
        generic = classify_candidate(c.get('text', ''))['generic']
        if signals >= 2 or not generic:
            strong.append(c)
        else:
            weak.append(c)
    return strong, weak


# ─── USER.md helpers ──────────────────────────────────────────────────────────

def read_usermd():
    if os.path.exists(USERMD_PATH):
        with open(USERMD_PATH, encoding='utf-8') as f:
            return f.read()
    return ''


def write_usermd(content):
    with open(USERMD_PATH, 'w', encoding='utf-8') as f:
        f.write(content)


def ensure_section(content, heading):
    """Append a section to USER.md if it doesn't exist."""
    if f'## {heading}' not in content:
        content = content.rstrip() + f'\n\n## {heading}\n\n'
    return content


def insert_after_section(content, heading, new_entry):
    """Insert new_entry immediately after the section heading line."""
    marker = f'## {heading}'
    if marker not in content:
        content = content.rstrip() + f'\n\n{marker}\n\n{new_entry}\n'
        return content
    # Find position after heading (and any blank line after it)
    idx = content.index(marker) + len(marker)
    # Skip to end of that line
    while idx < len(content) and content[idx] != '\n':
        idx += 1
    idx += 1  # past the newline
    # Insert after optional blank line
    if idx < len(content) and content[idx] == '\n':
        idx += 1
    return content[:idx] + new_entry + '\n' + content[idx:]


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_add_candidate(payload_str):
    payload = json.loads(payload_str)
    state = load_state()

    text = payload['text'].strip()
    source = payload.get('source', 'conversation')
    today = payload.get('date', str(date.today()))
    normalized_text = normalize_text(text)
    evidence_ids = [str(x).strip() for x in payload.get('evidence_ids', []) if str(x).strip()]
    fallback_evidence = payload.get('evidence', '').strip()
    evidence = format_signal_evidence(state, source, evidence_ids, fallback=fallback_evidence)
    new_signal = {
        'evidence': evidence,
        'source': source,
        'date': today,
        'evidence_ids': evidence_ids,
    }
    analysis = payload.get('analysis', '').strip()
    if analysis:
        new_signal['analysis'] = analysis

    # Check if a similar candidate already exists.
    for c in state['candidates']:
        existing_normalized = normalize_text(c.get('text', ''))
        existing_fingerprints = {evidence_fingerprint(sig) for sig in c.get('signals', []) if evidence_fingerprint(sig)}
        same_text = c['text'].lower() == text.lower()
        similar_text = jaccard(normalized_text, existing_normalized) >= 0.45
        same_evidence = evidence_fingerprint(new_signal) in existing_fingerprints
        if same_text or similar_text or same_evidence:
            # Avoid adding exact duplicate signals.
            for sig in c.get('signals', []):
                if (
                    (sig.get('source') or '').strip().lower() == source.strip().lower()
                    and (sig.get('date') or '').strip() == today
                    and evidence_fingerprint(sig) == evidence_fingerprint(new_signal)
                ):
                    if evidence_ids:
                        merged_ids = sorted(set(sig.get('evidence_ids', []) + evidence_ids))
                        sig['evidence_ids'] = merged_ids
                        save_state(state)
                    print(f'Skipped duplicate signal for existing candidate "{c["id"]}".')
                    return

            # Add a signal to the existing candidate
            c['signals'].append(new_signal)
            signal_count = len(c['signals'])
            save_state(state)
            print(f'Signal added to existing candidate "{c["id"]}" ({signal_count} signals total).')
            if signal_count >= SIGNALS_REQUIRED:
                print(f'⚡ Ready to confirm — run: manage-profile.py approve {c["id"]}')
            return

    # New candidate
    cid = str(uuid.uuid4())[:8]
    candidate = {
        'id': cid,
        'text': text,
        'signals': [new_signal],
        'created': today,
    }
    state['candidates'].append(candidate)
    save_state(state)
    print(f'Candidate added: {cid}')
    print(f'Text: {text}')
    print(f'Signals: 1/{SIGNALS_REQUIRED} needed to confirm')


def cmd_approve(cid):
    state = load_state()
    candidate = next((c for c in state['candidates'] if c['id'] == cid), None)
    if not candidate:
        print(f'ERROR: candidate {cid} not found', file=sys.stderr)
        sys.exit(1)

    signal_count = len(candidate['signals'])
    today = str(date.today())

    # Build the pattern entry for USER.md
    lines = [f'- **{candidate["text"]}** — *{signal_count} signals* — last: {today}']
    for sig in candidate['signals']:
        lines.append(f'  - {sig["source"]}: {sig["evidence"]}')
    entry = '\n'.join(lines)

    content = read_usermd()
    content = ensure_section(content, 'Observed Patterns')
    content = insert_after_section(content, 'Observed Patterns', entry)
    write_usermd(content)

    state['candidates'] = [c for c in state['candidates'] if c['id'] != cid]
    save_state(state)

    confirmed = '✓ Pattern confirmed and written to USER.md.' if signal_count >= SIGNALS_REQUIRED else \
                f'⚠ Written with only {signal_count} signal(s) — below the {SIGNALS_REQUIRED}-signal threshold.'
    print(confirmed)
    print(f'Pattern: {candidate["text"]}')


def cmd_reject(cid):
    state = load_state()
    before = len(state['candidates'])
    state['candidates'] = [c for c in state['candidates'] if c['id'] != cid]
    if len(state['candidates']) == before:
        print(f'ERROR: candidate {cid} not found', file=sys.stderr)
        sys.exit(1)
    save_state(state)
    print(f'Candidate {cid} rejected and removed.')


def cmd_list(verbose=False):
    state = load_state()
    candidates = unrated_candidates(state)
    if not candidates:
        print('No pending candidates.')
        return

    candidates = sorted(candidates, key=lambda c: (-candidate_score(c), c['created'], c['id']))
    strong, weak = group_by_quality(candidates)
    clusters = [cluster for cluster in find_duplicate_clusters(candidates) if len(cluster) > 1]

    print(f'{len(candidates)} pending candidate(s) total')
    print(f'- stronger: {len(strong)}')
    print(f'- weak/generic: {len(weak)}')
    print(f'- duplicate clusters: {len(clusters)}\n')

    if clusters:
        print('Likely duplicate clusters:')
        for cluster in clusters:
            ids = ', '.join(c['id'] for c in cluster)
            print(f'- {ids}')
            for c in cluster:
                print(f'  • {c["text"]}')
        print()

    print('Best candidates first:\n')
    for c in strong:
        sig_count = len(c['signals'])
        status = '✓ ready to approve' if sig_count >= SIGNALS_REQUIRED else f'{sig_count}/{SIGNALS_REQUIRED} signals'
        meta = classify_candidate(c.get('text', ''))
        source_diversity = len({(s.get('source') or '').strip().lower() for s in c.get('signals', []) if s.get('source')})
        print(f'[{c["id"]}] {c["text"]}')
        print(f'  Status: {status}  Created: {c["created"]}  Sources: {source_diversity}  Score: {candidate_score(c)}')
        for sig in c['signals']:
            print(f'  - {sig["source"]} ({sig["date"]}): {sig["evidence"]}')
        print()

    if weak:
        print('Weak/generic candidates:\n')
        for c in weak:
            sig_count = len(c['signals'])
            print(f'[{c["id"]}] {c["text"]}')
            print(f'  Status: {sig_count}/{SIGNALS_REQUIRED} signals  Created: {c["created"]}  Score: {candidate_score(c)}')
            if verbose:
                for sig in c['signals']:
                    print(f'  - {sig["source"]} ({sig["date"]}): {sig["evidence"]}')
            print()


def cmd_add_contradiction(payload_str):
    payload = json.loads(payload_str)
    a = payload['a'].strip()
    b = payload['b'].strip()
    evidence_a = payload.get('evidence_a', '').strip()
    evidence_b = payload.get('evidence_b', '').strip()
    same_domain = payload.get('same_domain', None)
    today = payload.get('date', str(date.today()))

    state = load_state()
    contradiction = {
        'a': a,
        'b': b,
        'evidence_a': evidence_a,
        'evidence_b': evidence_b,
        'same_domain': same_domain,
        'date': today,
    }
    bucket = state.setdefault('contradictions', [])
    if contradiction not in bucket:
        bucket.append(contradiction)
        save_state(state)

    domain_str = 'yes' if same_domain is True else ('no' if same_domain is False else '?')

    lines = [
        f'- **{a}** ↔ **{b}** — first: {today} — same domain: {domain_str}',
        f'  - A: {evidence_a}' if evidence_a else '  - A: (no evidence recorded)',
        f'  - B: {evidence_b}' if evidence_b else '  - B: (no evidence recorded)',
    ]
    entry = '\n'.join(lines)

    content = read_usermd()
    content = ensure_section(content, 'Contradictions & Dilemmas')
    content = insert_after_section(content, 'Contradictions & Dilemmas', entry)
    write_usermd(content)
    print(f'Contradiction added to USER.md:')
    print(f'  {a} ↔ {b}')


def normalize_bucket(bucket):
    bucket = (bucket or '').strip().lower()
    aliases = {
        'must': 'must',
        'must-have': 'must',
        'must_have': 'must',
        'nice': 'nice',
        'nice-to-have': 'nice',
        'nice_to_have': 'nice',
        'not': 'not',
        'never-mind': 'not',
        'never_mind': 'not',
        'nevermind': 'not',
    }
    return aliases.get(bucket, '')


def normalize_reinforcement_bucket(bucket):
    bucket = (bucket or '').strip().lower()
    aliases = {
        'valid': 'valid',
        'valid-reinforcement': 'valid',
        'weak': 'weak',
        'weak-reinforcement': 'weak',
        'not': 'not',
        'not-really': 'not',
        'not_really': 'not',
        'review': 'review',
    }
    return aliases.get(bucket, '')


def candidate_feedback_record(cid, bucket, schema='user_profiler.feedback.v1'):
    state = load_state()
    candidate = next((c for c in state['candidates'] if c['id'] == cid), None)
    if not candidate:
        print(f'ERROR: candidate {cid} not found', file=sys.stderr)
        sys.exit(1)

    last_sig = candidate.get('signals', [])[-1] if candidate.get('signals') else {}
    rec = {
        'schema': schema,
        'candidate_id': cid,
        'bucket_user': bucket,
        'text': candidate.get('text', ''),
        'evidence': last_sig.get('evidence', ''),
        'source': last_sig.get('source', ''),
        'date': last_sig.get('date', str(date.today())),
    }
    analysis = last_sig.get('analysis', '')
    if analysis:
        rec['analysis'] = analysis
    if last_sig.get('evidence_ids'):
        rec['evidence_ids'] = list(last_sig.get('evidence_ids', []))
    return rec


def cmd_rate(cid, bucket):
    bucket = normalize_bucket(bucket)
    if bucket not in {'must', 'nice', 'not'}:
        print('ERROR: bucket must be one of: must | nice | not | must-have | nice-to-have | never-mind', file=sys.stderr)
        sys.exit(1)

    rec = candidate_feedback_record(cid, bucket, schema='user_profiler.feedback.v1')
    append_feedback(rec)
    print(f'Rated {cid} as {bucket}. Saved to {FEEDBACK_PATH}.')


def cmd_flag_review(cid):
    rec = candidate_feedback_record(cid, 'review', schema='user_profiler.review_flag.v1')
    append_feedback(rec)
    print(f'Flagged {cid} for review. Saved to {FEEDBACK_PATH}.')


def cmd_rate_reinforcement(rid, bucket):
    bucket = normalize_reinforcement_bucket(bucket)
    if bucket not in {'valid', 'weak', 'not', 'review'}:
        print('ERROR: bucket must be one of: valid | weak | not | review | valid-reinforcement | weak-reinforcement | not-really', file=sys.stderr)
        sys.exit(1)

    state = load_state()
    reinforcements = state.get('reinforcements', [])
    if rid.isdigit():
        idx = int(rid)
        if idx < 0 or idx >= len(reinforcements):
            print(f'ERROR: reinforcement index {rid} not found', file=sys.stderr)
            sys.exit(1)
        item = reinforcements[idx]
        reinforcement_id = str(idx)
    else:
        print('ERROR: reinforcement id currently expects numeric index', file=sys.stderr)
        sys.exit(1)

    schema = 'user_profiler.reinforcement_feedback.v1' if bucket != 'review' else 'user_profiler.reinforcement_review_flag.v1'
    rec = {
        'schema': schema,
        'reinforcement_id': reinforcement_id,
        'bucket_user': bucket,
        'pattern': item.get('pattern', ''),
        'evidence': item.get('evidence', ''),
        'source': item.get('source', ''),
        'date': item.get('date', str(date.today())),
    }
    append_feedback(rec)
    print(f'Rated reinforcement {reinforcement_id} as {bucket}. Saved to {FEEDBACK_PATH}.')


def cmd_show():
    content = read_usermd()
    sections = ['Observed Patterns', 'Contradictions & Dilemmas']
    for section in sections:
        marker = f'## {section}'
        if marker in content:
            start = content.index(marker)
            # Find next ## section or end of file
            rest = content[start:]
            next_section = re.search(r'\n## ', rest[3:])
            end = (start + 3 + next_section.start()) if next_section else len(content)
            print(content[start:end].strip())
            print()
        else:
            print(f'[{section}: not yet in USER.md]')
            print()


def evidence_is_summary_like(evidence):
    text = (evidence or '').strip()
    lowered = text.lower()
    bad_markers = [
        "mauro highlights",
        "mauro clips",
        "in '",
        'in "',
        '[notion] snip:',
        'podcast',
        'additionally,',
        'particularly how',
    ]
    return any(marker in lowered for marker in bad_markers)


def select_review_signal(candidate):
    signals = list(candidate.get('signals', []))
    if not signals:
        return None
    literal = [sig for sig in reversed(signals) if not evidence_is_summary_like(sig.get('evidence', ''))]
    if literal:
        return literal[0]
    return None


def reviewable_candidates(state):
    out = []
    for c in unrated_candidates(state):
        sig = select_review_signal(c)
        if sig:
            out.append((c, sig))
    return out


def cmd_review():
    state = load_state()
    items = sorted(reviewable_candidates(state), key=lambda item: (-candidate_score(item[0]), item[0]['created'], item[0]['id']))
    if not items:
        print('No pending candidates.')
        return
    print('Review queue:\n')
    for c, sig in items:
        print(f'[{c["id"]}] {c["text"]}')
        print(f'  Rate with one of: must-have | nice-to-have | never-mind')
        print(f'  Evidence: {sig.get("evidence", "")}')
        print()


def cmd_review_batch(n=4):
    state = load_state()
    items = sorted(reviewable_candidates(state), key=lambda item: (-candidate_score(item[0]), item[0]['created'], item[0]['id']))
    if not items:
        print('No pending candidates.')
        return
    for c, sig in items[:max(1, n)]:
        print('Type: candidate')
        print(f'ID: {c["id"]}')
        print(f'Text: {c["text"]}')
        print(f'Evidence: {sig.get("evidence", "")}')
        print('Buttons:')
        print(f'  must-have -> profile_rate:candidate:{c["id"]}:must')
        print(f'  nice-to-have -> profile_rate:candidate:{c["id"]}:nice')
        print(f'  never-mind -> profile_rate:candidate:{c["id"]}:not')
        print(f'  review -> profile_rate:candidate:{c["id"]}:review')
        print()


def cmd_reinforcement_review_batch(n=4):
    state = load_state()
    items = list(state.get('reinforcements', []))
    if not items:
        print('No reinforcements.')
        return
    for idx, item in list(enumerate(items))[:max(1, n)]:
        print('Type: reinforcement')
        print(f'ID: {idx}')
        print(f'Text: {item.get("pattern", "")}')
        print(f'Evidence: {item.get("evidence", "")}')
        print('Buttons:')
        print(f'  valid -> profile_rate:reinforcement:{idx}:valid')
        print(f'  weak -> profile_rate:reinforcement:{idx}:weak')
        print(f'  not-really -> profile_rate:reinforcement:{idx}:not')
        print(f'  review -> profile_rate:reinforcement:{idx}:review')
        print()


def cmd_contradiction_review_batch(n=4):
    state = load_state()
    items = list(state.get('contradictions', []))
    if not items:
        print('No contradictions.')
        return
    for idx, item in list(enumerate(items))[:max(1, n)]:
        print('Type: contradiction')
        print(f'ID: {idx}')
        print(f'Text: {item.get("a", "")} ↔ {item.get("b", "")}')
        evidence = f'A: {item.get("evidence_a", "")} | B: {item.get("evidence_b", "")}'
        print(f'Evidence: {evidence}')
        print('Buttons:')
        print(f'  valid -> profile_rate:contradiction:{idx}:valid')
        print(f'  weak -> profile_rate:contradiction:{idx}:weak')
        print(f'  not-really -> profile_rate:contradiction:{idx}:not')
        print(f'  review -> profile_rate:contradiction:{idx}:review')
        print()


def cmd_rate_contradiction(rid, bucket):
    bucket = normalize_reinforcement_bucket(bucket)
    if bucket not in {'valid', 'weak', 'not', 'review'}:
        print('ERROR: bucket must be one of: valid | weak | not | review', file=sys.stderr)
        sys.exit(1)
    state = load_state()
    contradictions = state.get('contradictions', [])
    if not rid.isdigit() or int(rid) < 0 or int(rid) >= len(contradictions):
        print(f'ERROR: contradiction index {rid} not found', file=sys.stderr)
        sys.exit(1)
    item = contradictions[int(rid)]
    schema = 'user_profiler.contradiction_feedback.v1' if bucket != 'review' else 'user_profiler.contradiction_review_flag.v1'
    rec = {
        'schema': schema,
        'bucket_user': bucket,
        'target': f'contradiction:{rid}',
        'text': f'{item.get("a", "")} ↔ {item.get("b", "")}',
        'evidence': f'A: {item.get("evidence_a", "")} | B: {item.get("evidence_b", "")}',
        'source': 'contradiction',
        'date': item.get('date', str(date.today())),
    }
    append_feedback(rec)
    print(f'Rated contradiction {rid} as {bucket}. Saved to {FEEDBACK_PATH}.')


def cmd_rate_callback(payload):
    payload = (payload or '').strip()
    m = re.fullmatch(r'profile_rate:(candidate|reinforcement|contradiction):([a-z0-9]{1,32}):(must|nice|not|review|valid|weak)', payload)
    if not m:
        print('ERROR: expected profile_rate:<candidate|reinforcement|contradiction>:<id>:<bucket>', file=sys.stderr)
        sys.exit(1)
    item_type, item_id, bucket = m.group(1), m.group(2), m.group(3)
    if item_type == 'candidate':
        if bucket == 'review':
            cmd_flag_review(item_id)
        else:
            cmd_rate(item_id, bucket)
    elif item_type == 'reinforcement':
        cmd_rate_reinforcement(item_id, bucket)
    else:
        cmd_rate_contradiction(item_id, bucket)


def cmd_rate_callback_json(payload):
    payload = (payload or '').strip()
    m = re.fullmatch(r'profile_rate:(candidate|reinforcement|contradiction):([a-z0-9]{1,32}):(must|nice|not|review|valid|weak)', payload)
    if not m:
        print(json.dumps({'ok': False, 'error': 'invalid_payload'}))
        sys.exit(1)
    item_type, item_id, bucket = m.group(1), m.group(2), m.group(3)
    if item_type == 'candidate':
        if bucket == 'review':
            cmd_flag_review(item_id)
        else:
            cmd_rate(item_id, bucket)
    elif item_type == 'reinforcement':
        cmd_rate_reinforcement(item_id, bucket)
    else:
        cmd_rate_contradiction(item_id, bucket)
    mapping = load_review_messages()
    print(json.dumps({
        'ok': True,
        'item_type': item_type,
        'item_id': item_id,
        'bucket': bucket,
        'message_id': mapping.get(profile_review_mapping_key(item_type, item_id)),
    }, ensure_ascii=False))


def cmd_review_register(cid, message_id):
    mapping = load_review_messages()
    key = cid if ':' in cid else profile_review_mapping_key('candidate', cid)
    mapping[key] = str(message_id)
    save_review_messages(mapping)
    print(f'Registered review card {key} -> {message_id}')


def cmd_review_message(cid):
    mapping = load_review_messages()
    key = cid if ':' in cid else profile_review_mapping_key('candidate', cid)
    msg = mapping.get(key)
    if not msg:
        print('')
        return
    print(str(msg))


def cmd_review_clear(cid):
    mapping = load_review_messages()
    key = cid if ':' in cid else profile_review_mapping_key('candidate', cid)
    old = mapping.pop(key, None)
    save_review_messages(mapping)
    if old:
        print(f'Cleared review card {key} -> {old}')
    else:
        print(f'No review card found for {key}')


def cmd_dedupe():
    state = load_state()
    candidates = list(state.get('candidates', []))
    if not candidates:
        print('No pending candidates.')
        return

    clusters = [cluster for cluster in find_duplicate_clusters(candidates) if len(cluster) > 1]
    if not clusters:
        print('No obvious duplicates found.')
        return

    merged = []
    consumed = set()
    merge_count = 0

    for cluster in clusters:
        cluster_ids = {c['id'] for c in cluster}
        consumed |= cluster_ids
        best = sorted(cluster, key=lambda c: (-candidate_score(c), c['created'], c['id']))[0]
        all_signals = []
        seen = set()
        for c in sorted(cluster, key=lambda c: (c['created'], c['id'])):
            for sig in c.get('signals', []):
                key = (
                    (sig.get('source') or '').strip().lower(),
                    (sig.get('date') or '').strip(),
                    evidence_fingerprint(sig),
                )
                if key in seen:
                    continue
                seen.add(key)
                all_signals.append(sig)
        best = dict(best)
        best['signals'] = all_signals
        best['created'] = min(c.get('created', best['created']) for c in cluster)
        merged.append(best)
        merge_count += len(cluster) - 1

    for c in candidates:
        if c['id'] not in consumed:
            merged.append(c)

    state['candidates'] = sorted(merged, key=lambda c: (-candidate_score(c), c['created'], c['id']))
    save_state(state)
    print(f'Merged {merge_count} duplicate candidate(s) across {len(clusters)} cluster(s).')
    print(f'{len(state["candidates"])} pending candidate(s) remain.')


def find_evidence_ids_for_candidate(state, candidate):
    explicit = []
    for sig in candidate.get('signals', []):
        explicit.extend([x for x in sig.get('evidence_ids', []) if x])
    explicit = sorted(set(explicit))
    if explicit:
        return explicit

    evidence = state.get('evidence', {})
    matches = []
    signal_fingerprints = []
    for sig in candidate.get('signals', []):
        fp = evidence_fingerprint(sig)
        if fp:
            signal_fingerprints.append(fp)
    for evidence_id, rec in evidence.items():
        rec_fp = evidence_record_fingerprint(rec)
        if not rec_fp:
            continue
        for fp in signal_fingerprints:
            if fp == rec_fp or jaccard(fp, rec_fp) >= 0.5:
                matches.append(evidence_id)
                break
    return sorted(set(matches))


def cmd_memory_input(cid):
    state = load_state()
    candidate = next((c for c in state['candidates'] if c['id'] == cid), None)
    if not candidate:
        print(f'ERROR: candidate {cid} not found', file=sys.stderr)
        sys.exit(1)
    lines = [candidate.get('text', '').strip()]
    for sig in candidate.get('signals', []):
        evidence = (sig.get('evidence') or '').strip()
        source = (sig.get('source') or '').strip()
        if evidence:
            lines.append(f'- {source}: {evidence}' if source else f'- {evidence}')
    for evidence_id in find_evidence_ids_for_candidate(state, candidate):
        lines.append(f'EVIDENCE_ID: {evidence_id}')
    print('\n'.join(lines).strip())


def cmd_evidence_summary():
    state = load_state()
    evidence = state.get('evidence', {})
    if not evidence:
        print('No evidence records.')
        return
    counts = defaultdict(int)
    source_counts = defaultdict(int)
    for rec in evidence.values():
        counts[infer_evidence_status(rec)] += 1
        source_counts[(rec.get('source') or 'unknown')] += 1
    print(f'{len(evidence)} evidence record(s) total')
    for status in ['pending', 'analyzed', 'promoted_to_memory', 'discarded']:
        print(f'- {status}: {counts.get(status, 0)}')
    print('\nBy source:')
    for source in sorted(source_counts):
        print(f'- {source}: {source_counts[source]}')


def cmd_resolve_evidence(evidence_id, status, memory_date=None):
    state = load_state()
    if status not in {'promoted_to_memory', 'discarded'}:
        print('ERROR: status must be promoted_to_memory or discarded', file=sys.stderr)
        sys.exit(1)
    rec = state.get('evidence', {}).get(evidence_id)
    if not rec:
        print(f'ERROR: evidence {evidence_id} not found', file=sys.stderr)
        sys.exit(1)
    today = memory_date or str(date.today())
    rec['status'] = status
    rec['resolved_at'] = today
    if status == 'promoted_to_memory':
        rec['memory_written_at'] = today
        rec.pop('discarded_at', None)
    else:
        rec['discarded_at'] = today
        rec.pop('memory_written_at', None)
    state['evidence'][evidence_id] = compact_evidence_record(rec)
    save_state(state)
    print(f'{evidence_id} -> {status}')


def cmd_compact_evidence():
    state = load_state()
    evidence = state.get('evidence', {})
    changed = 0
    for evidence_id, rec in list(evidence.items()):
        status = infer_evidence_status(rec)
        if status not in {'promoted_to_memory', 'discarded'}:
            continue
        compacted = compact_evidence_record(rec)
        if compacted != rec:
            evidence[evidence_id] = compacted
            changed += 1
    save_state(state)
    print(f'Compacted {changed} resolved evidence record(s).')


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0]

    if cmd == 'add-candidate':
        if len(args) < 2:
            print('Usage: manage-profile.py add-candidate \'{"text":...}\'', file=sys.stderr)
            sys.exit(1)
        cmd_add_candidate(args[1])

    elif cmd == 'approve':
        if len(args) < 2:
            print('Usage: manage-profile.py approve <id>', file=sys.stderr)
            sys.exit(1)
        cmd_approve(args[1])

    elif cmd == 'reject':
        if len(args) < 2:
            print('Usage: manage-profile.py reject <id>', file=sys.stderr)
            sys.exit(1)
        cmd_reject(args[1])

    elif cmd == 'rate':
        if len(args) < 3:
            print('Usage: manage-profile.py rate <id> <must|nice|not|must-have|nice-to-have|never-mind>', file=sys.stderr)
            sys.exit(1)
        cmd_rate(args[1], args[2])

    elif cmd == 'rate-callback':
        if len(args) < 2:
            print('Usage: manage-profile.py rate-callback profile_rate:<candidate-id>:<must|nice|not|review>', file=sys.stderr)
            sys.exit(1)
        cmd_rate_callback(args[1])

    elif cmd == 'rate-callback-json':
        if len(args) < 2:
            print('Usage: manage-profile.py rate-callback-json profile_rate:<candidate-id>:<must|nice|not|review>', file=sys.stderr)
            sys.exit(1)
        cmd_rate_callback_json(args[1])

    elif cmd == 'contradiction-review-batch':
        n = int(args[1]) if len(args) > 1 else 4
        cmd_contradiction_review_batch(n)

    elif cmd == 'review-register':
        if len(args) < 3:
            print('Usage: manage-profile.py review-register <candidate-id> <message-id>', file=sys.stderr)
            sys.exit(1)
        cmd_review_register(args[1], args[2])

    elif cmd == 'review-message':
        if len(args) < 2:
            print('Usage: manage-profile.py review-message <candidate-id>', file=sys.stderr)
            sys.exit(1)
        cmd_review_message(args[1])

    elif cmd == 'review-clear':
        if len(args) < 2:
            print('Usage: manage-profile.py review-clear <candidate-id>', file=sys.stderr)
            sys.exit(1)
        cmd_review_clear(args[1])

    elif cmd == 'list':
        cmd_list('--verbose' in args[1:])

    elif cmd == 'review':
        cmd_review()

    elif cmd == 'review-batch':
        n = 4
        if len(args) >= 2:
            try:
                n = int(args[1])
            except ValueError:
                print('ERROR: review-batch [n] expects integer n', file=sys.stderr)
                sys.exit(1)
        cmd_review_batch(n)

    elif cmd == 'reinforcement-review-batch':
        n = 4
        if len(args) >= 2:
            try:
                n = int(args[1])
            except ValueError:
                print('ERROR: reinforcement-review-batch [n] expects integer n', file=sys.stderr)
                sys.exit(1)
        cmd_reinforcement_review_batch(n)

    elif cmd == 'dedupe':
        cmd_dedupe()

    elif cmd == 'memory-input':
        if len(args) < 2:
            print('Usage: manage-profile.py memory-input <candidate-id>', file=sys.stderr)
            sys.exit(1)
        cmd_memory_input(args[1])

    elif cmd == 'evidence-summary':
        cmd_evidence_summary()

    elif cmd == 'resolve-evidence':
        if len(args) < 3:
            print('Usage: manage-profile.py resolve-evidence <id> <promoted_to_memory|discarded> [--memory-date YYYY-MM-DD]', file=sys.stderr)
            sys.exit(1)
        memory_date = None
        if '--memory-date' in args[3:]:
            idx = args.index('--memory-date')
            if idx + 1 >= len(args):
                print('ERROR: --memory-date requires YYYY-MM-DD', file=sys.stderr)
                sys.exit(1)
            memory_date = args[idx + 1]
        cmd_resolve_evidence(args[1], args[2], memory_date)

    elif cmd == 'compact-evidence':
        cmd_compact_evidence()

    elif cmd == 'add-contradiction':
        if len(args) < 2:
            print('Usage: manage-profile.py add-contradiction \'{"a":...,"b":...}\'', file=sys.stderr)
            sys.exit(1)
        cmd_add_contradiction(args[1])

    elif cmd == 'show':
        cmd_show()

    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        print('Commands: add-candidate, approve, reject, rate, rate-callback, rate-callback-json, review-register, review-message, review-clear, list, review, review-batch, reinforcement-review-batch, contradiction-review-batch, dedupe, memory-input, evidence-summary, resolve-evidence, compact-evidence, add-contradiction, show')
        sys.exit(1)


if __name__ == '__main__':
    main()
