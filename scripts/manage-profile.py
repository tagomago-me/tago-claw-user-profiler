#!/usr/bin/env python3
"""
Manage behavioral profile: candidates, patterns, contradictions in USER.md.

Commands:
  add-candidate '<json>'       Add a behavioral signal candidate
  approve <id>                 Approve a candidate (writes to USER.md at 4+ signals)
  reject <id>                  Remove a candidate
  rate <id> <must|nice|not>    Rate a candidate's relevance (feedback loop)
  list                         Show all pending candidates
  add-contradiction '<json>'   Add a contradiction directly to USER.md
  show                         Show current patterns and contradictions from USER.md

JSON for add-candidate:
  {"text": "...", "evidence": "...", "source": "conversation|nostr", "date": "YYYY-MM-DD"}

JSON for add-contradiction:
  {"a": "...", "b": "...", "evidence_a": "...", "evidence_b": "...",
   "same_domain": true|false, "date": "YYYY-MM-DD"}
"""

import sys
import os
import json
import re
import uuid
from datetime import date

USERMD_PATH = '/data/USER.md'
STATE_PATH = '/data/memory/profiler-state.json'
FEEDBACK_PATH = '/data/memory/profile-scoring-feedback.jsonl'
SIGNALS_REQUIRED = 4

# ─── State file ───────────────────────────────────────────────────────────────

def load_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding='utf-8') as f:
            state = json.load(f)
    state.setdefault('candidates', [])
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_feedback(rec):
    os.makedirs(os.path.dirname(FEEDBACK_PATH), exist_ok=True)
    with open(FEEDBACK_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


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
    evidence = payload.get('evidence', '').strip()
    source = payload.get('source', 'conversation')
    today = payload.get('date', str(date.today()))

    # Check if a similar candidate already exists (same text, case-insensitive)
    for c in state['candidates']:
        if c['text'].lower() == text.lower():
            # Add a signal to the existing candidate
            c['signals'].append({'evidence': evidence, 'source': source, 'date': today})
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
        'signals': [{'evidence': evidence, 'source': source, 'date': today}],
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


def cmd_list():
    state = load_state()
    if not state['candidates']:
        print('No pending candidates.')
        return
    print(f'{len(state["candidates"])} pending candidate(s):\n')
    for c in state['candidates']:
        sig_count = len(c['signals'])
        status = '✓ ready to approve' if sig_count >= SIGNALS_REQUIRED else f'{sig_count}/{SIGNALS_REQUIRED} signals'
        print(f'[{c["id"]}] {c["text"]}')
        print(f'  Status: {status}  Created: {c["created"]}')
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


def cmd_rate(cid, bucket):
    bucket = (bucket or '').strip().lower()
    if bucket not in {'must', 'nice', 'not'}:
        print('ERROR: bucket must be one of: must | nice | not', file=sys.stderr)
        sys.exit(1)

    state = load_state()
    candidate = next((c for c in state['candidates'] if c['id'] == cid), None)
    if not candidate:
        print(f'ERROR: candidate {cid} not found', file=sys.stderr)
        sys.exit(1)

    last_sig = candidate.get('signals', [])[-1] if candidate.get('signals') else {}
    rec = {
        'schema': 'user_profiler.feedback.v1',
        'candidate_id': cid,
        'bucket_user': bucket,
        'text': candidate.get('text', ''),
        'evidence': last_sig.get('evidence', ''),
        'source': last_sig.get('source', ''),
        'date': last_sig.get('date', str(date.today())),
    }
    append_feedback(rec)
    print(f'Rated {cid} as {bucket}. Saved to {FEEDBACK_PATH}.')


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
            print('Usage: manage-profile.py rate <id> <must|nice|not>', file=sys.stderr)
            sys.exit(1)
        cmd_rate(args[1], args[2])

    elif cmd == 'list':
        cmd_list()

    elif cmd == 'add-contradiction':
        if len(args) < 2:
            print('Usage: manage-profile.py add-contradiction \'{"a":...,"b":...}\'', file=sys.stderr)
            sys.exit(1)
        cmd_add_contradiction(args[1])

    elif cmd == 'show':
        cmd_show()

    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        print('Commands: add-candidate, approve, reject, rate, list, add-contradiction, show')
        sys.exit(1)


if __name__ == '__main__':
    main()
