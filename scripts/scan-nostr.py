#!/usr/bin/env python3
"""
Scan Nostr notes for behavioral signals (user-profiler heartbeat job).

Pulls 5 recent public notes from Mauro's Nostr relays, reads the existing
USER.md profile, then uses GPT-4o to find reinforcements, contradictions,
and new candidate behavioral signals.

Requires: OPENAI_API_KEY in environment, nak on PATH.
State: /data/memory/profiler-state.json  (key: lastNostrScan)
"""

import sys
import os
import json
import subprocess
import ssl
import urllib.request
import time
from datetime import date, datetime

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

USERMD_PATH  = '/data/USER.md'
STATE_PATH   = '/data/memory/profiler-state.json'
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MANAGE       = os.path.join(SCRIPT_DIR, 'manage-profile.py')
RELAYS       = ['wss://nostr.tagomago.me', 'wss://bridge.tagomago.me']
MIN_INTERVAL = 3   # days


# ─── State ────────────────────────────────────────────────────────────────────

def load_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)

def too_soon(state, key='lastNostrScan'):
    last = state.get(key)
    return bool(last) and (time.time() - last) / 86400 < MIN_INTERVAL


# ─── Nostr ────────────────────────────────────────────────────────────────────

def get_pubkey():
    key = os.environ.get('NOSTR_DAMUS_PUBLIC_HEX_KEY', '').strip()
    if not key:
        priv = os.environ.get('NOSTR_DAMUS_PRIVATE_HEX_KEY', '').strip()
        if priv:
            r = subprocess.run(['nak', 'key', 'public', priv], capture_output=True, text=True, timeout=10)
            key = r.stdout.strip()
    return key

def fetch_notes(pubkey, count=5):
    try:
        r = subprocess.run(
            ['nak', 'req', '-k', '1', '-a', pubkey, '-l', str(count)] + RELAYS,
            capture_output=True, text=True, timeout=30
        )
        events, seen = [], set()
        for line in r.stdout.splitlines():
            try:
                ev = json.loads(line.strip())
                if ev.get('kind') == 1 and ev.get('pubkey') == pubkey and ev['id'] not in seen:
                    seen.add(ev['id'])
                    events.append(ev)
            except Exception:
                pass
        return sorted(events, key=lambda e: e.get('created_at', 0), reverse=True)[:count]
    except Exception as e:
        print(f'Nostr fetch error: {e}', file=sys.stderr)
        return []


# ─── OpenAI ───────────────────────────────────────────────────────────────────

def call_openai(prompt):
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        print('ERROR: OPENAI_API_KEY not set', file=sys.stderr)
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps({'model': 'gpt-4o', 'max_tokens': 800, 'temperature': 0.3,
                         'messages': [{'role': 'user', 'content': prompt}]}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
    )
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'OpenAI error: {e}', file=sys.stderr)
        return None


# ─── Profile ──────────────────────────────────────────────────────────────────

def read_profile():
    import re
    if not os.path.exists(USERMD_PATH):
        return '(no profile yet)'
    with open(USERMD_PATH) as f:
        content = f.read()
    sections = []
    for heading in ['Observed Patterns', 'Contradictions & Dilemmas']:
        marker = f'## {heading}'
        if marker in content:
            start = content.index(marker)
            rest = content[start:]
            nxt = re.search(r'\n## ', rest[3:])
            end = (start + 3 + nxt.start()) if nxt else len(content)
            sections.append(content[start:end].strip())
    return '\n\n'.join(sections) if sections else '(no profile yet)'

def parse_json_response(raw):
    raw = raw.strip()
    if raw.startswith('```'):
        raw = '\n'.join(raw.split('\n')[1:])
    if raw.endswith('```'):
        raw = raw[:-3]
    try:
        return json.loads(raw.strip())
    except Exception as e:
        print(f'JSON parse error: {e}\nRaw: {raw[:200]}', file=sys.stderr)
        return None

def apply(analysis, today, source_tag):
    counts = {'reinforcements': 0, 'contradictions': 0, 'new_candidates': 0}
    for r in analysis.get('reinforcements', []):
        pattern, evidence = r.get('pattern','').strip(), r.get('evidence','').strip()
        if pattern and evidence:
            subprocess.run(['python3', MANAGE, 'add-candidate',
                json.dumps({'text': pattern, 'evidence': f'[{source_tag}] {evidence}',
                            'source': source_tag, 'date': today})], capture_output=True)
            counts['reinforcements'] += 1
    for c in analysis.get('contradictions', []):
        a, b = c.get('a','').strip(), c.get('b','').strip()
        if a and b:
            subprocess.run(['python3', MANAGE, 'add-contradiction',
                json.dumps({'a': a, 'b': b, 'evidence_a': c.get('evidence_a',''),
                            'evidence_b': c.get('evidence_b',''),
                            'same_domain': c.get('same_domain', None), 'date': today})], capture_output=True)
            counts['contradictions'] += 1
    for nc in analysis.get('new_candidates', []):
        text, evidence = nc.get('text','').strip(), nc.get('evidence','').strip()
        if text and evidence:
            subprocess.run(['python3', MANAGE, 'add-candidate',
                json.dumps({'text': text, 'evidence': f'[{source_tag}] {evidence}',
                            'source': source_tag, 'date': today})], capture_output=True)
            counts['new_candidates'] += 1
    return counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    today = str(date.today())

    if too_soon(state, 'lastNostrScan'):
        last = datetime.fromtimestamp(state['lastNostrScan']).strftime('%Y-%m-%d')
        print(f'Skipping: last Nostr scan was {last} (min interval: {MIN_INTERVAL}d)')
        return

    pubkey = get_pubkey()
    if not pubkey:
        print('ERROR: Nostr public key not found', file=sys.stderr)
        sys.exit(1)

    notes = fetch_notes(pubkey)
    if not notes:
        print('No Nostr notes fetched.')
        return
    print(f'Fetched {len(notes)} Nostr notes')

    profile = read_profile()
    notes_text = '\n\n'.join(
        f'[{datetime.fromtimestamp(n.get("created_at",0)).strftime("%Y-%m-%d")}]\n{n.get("content","")}'
        for n in notes
    )

    prompt = f"""You are building a behavioral profile of Mauro from evidence — not self-descriptions.

These are his recent public Nostr notes (spontaneous, written for an audience):

{notes_text}

EXISTING PROFILE:
{profile}

Look for:
1. Evidence SUPPORTING an existing pattern (quote the pattern exactly)
2. Evidence CONTRADICTING an existing pattern
3. NEW behavioral signals (grounded in a decision or observable preference, never a self-description)

Return ONLY valid JSON, no markdown:
{{"reinforcements":[{{"pattern":"...","evidence":"..."}}],
  "contradictions":[{{"a":"...","b":"...","evidence_a":"...","evidence_b":"...","same_domain":true}}],
  "new_candidates":[{{"text":"...","evidence":"..."}}]}}

Be conservative. 0 entries is fine. No hallucination."""

    raw = call_openai(prompt)
    if not raw:
        return

    analysis = parse_json_response(raw)
    if not analysis:
        return

    counts = apply(analysis, today, 'nostr')
    # Reload state to preserve candidates written by manage-profile.py subprocesses
    state = load_state()
    state['lastNostrScan'] = time.time()
    save_state(state)

    print(f'Done: {counts["reinforcements"]} reinforcements, {counts["contradictions"]} contradictions, {counts["new_candidates"]} new candidates')

if __name__ == '__main__':
    main()
