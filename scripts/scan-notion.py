#!/usr/bin/env python3
"""
Scan Notion (Readwise + Snipd) for behavioral signals (user-profiler heartbeat job).

Sources:
  - Readwise Library: highlights from the 3 most recently read books
  - Snipd: snips from the 3 most recently clipped podcast episodes
    (includes AI summary bullets + transcript excerpt per snip)

Reads the existing USER.md profile, then uses GPT-4o to find reinforcements,
contradictions, and new candidate behavioral signals.

Requires: OPENAI_API_KEY, NOTION_API_KEY in environment.
State: /data/memory/profiler-state.json  (key: lastNotionScan)
"""

import sys
import os
import re
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
MIN_INTERVAL = 3   # days

NOTION_VERSION       = '2022-06-28'
READWISE_LIBRARY_DB  = '1ea2c1e8779c8137b03fe00b8b94392e'
SNIPD_DB             = '1ea2c1e8779c80c48030ddd64a701758'


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

def too_soon(state, key='lastNotionScan'):
    last = state.get(key)
    return bool(last) and (time.time() - last) / 86400 < MIN_INTERVAL


# ─── HTTP / Notion ────────────────────────────────────────────────────────────

def _opener():
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))

def notion_get(path):
    key = os.environ.get('NOTION_API_KEY', '')
    req = urllib.request.Request(
        f'https://api.notion.com/v1/{path}',
        headers={'Authorization': f'Bearer {key}', 'Notion-Version': NOTION_VERSION}
    )
    with _opener().open(req, timeout=20) as r:
        return json.loads(r.read())

def notion_post(path, payload):
    key = os.environ.get('NOTION_API_KEY', '')
    req = urllib.request.Request(
        f'https://api.notion.com/v1/{path}',
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {key}', 'Notion-Version': NOTION_VERSION,
                 'Content-Type': 'application/json'}
    )
    with _opener().open(req, timeout=20) as r:
        return json.loads(r.read())

def rich_text(prop):
    """Extract plain text from a rich_text or title property value."""
    return ''.join(x.get('plain_text', '') for x in prop)

def block_text(block):
    """Extract plain text from a single block."""
    btype = block.get('type', '')
    rt = block.get(btype, {}).get('rich_text', [])
    return rich_text(rt)


# ─── Readwise ─────────────────────────────────────────────────────────────────

def fetch_readwise(n_books=3, highlights_per_book=6):
    """
    Return list of:
      {"title": str, "author": str, "highlights": [str]}
    """
    try:
        data = notion_post(f'databases/{READWISE_LIBRARY_DB}/query', {
            'page_size': n_books,
            'sorts': [{'property': 'Last Highlighted', 'direction': 'descending'}],
            'filter': {'property': 'Category', 'select': {'equals': 'Books'}},
        })
    except Exception as e:
        print(f'Readwise query error: {e}', file=sys.stderr)
        return []

    books = []
    for page in data.get('results', []):
        props = page.get('properties', {})
        title = rich_text(props.get('Full Title', {}).get('rich_text', [])) or \
                rich_text(props.get('Title', {}).get('title', []))
        author = rich_text(props.get('Author', {}).get('rich_text', []))

        try:
            children = notion_get(f'blocks/{page["id"]}/children?page_size=50')
        except Exception:
            continue

        highlights = []
        for block in children.get('results', []):
            text = block_text(block).strip()
            if text and len(text) > 20 and not text.startswith('http'):
                highlights.append(text)
            if len(highlights) >= highlights_per_book:
                break

        if highlights:
            books.append({'title': title, 'author': author, 'highlights': highlights})

    return books


# ─── Snipd ────────────────────────────────────────────────────────────────────

def fetch_snipd(n_episodes=3, snips_per_episode=4, transcript_lines=6):
    """
    Return list of:
      {"episode": str, "show": str,
       "snips": [{"title": str, "summary": [str], "transcript": str}]}
    """
    try:
        data = notion_post(f'databases/{SNIPD_DB}/query', {
            'page_size': n_episodes,
            'sorts': [{'property': 'Last snip date', 'direction': 'descending'}],
        })
    except Exception as e:
        print(f'Snipd query error: {e}', file=sys.stderr)
        return []

    episodes = []
    for page in data.get('results', []):
        props = page.get('properties', {})
        episode = rich_text(props.get('Episode', {}).get('title', []))
        show    = rich_text(props.get('Show', {}).get('rich_text', []))

        try:
            ep_children = notion_get(f'blocks/{page["id"]}/children?page_size=50')
        except Exception:
            continue

        snips = []
        for block in ep_children.get('results', []):
            if block.get('type') != 'heading_3':
                continue

            # Snip title (strip timestamp prefix)
            title = re.sub(r'^\[\d+:\d+\]\s*', '', rich_text(block['heading_3']['rich_text']))
            if not title:
                continue

            summary_bullets = []
            transcript_text = ''

            try:
                snip_children = notion_get(f'blocks/{block["id"]}/children?page_size=20')
            except Exception:
                snips.append({'title': title, 'summary': [], 'transcript': ''})
                continue

            for sb in snip_children.get('results', []):
                stype = sb.get('type', '')

                if stype == 'bulleted_list_item':
                    bullet = block_text(sb).strip()
                    if bullet:
                        summary_bullets.append(bullet)

                elif stype == 'toggle':
                    toggle_label = rich_text(sb.get('toggle', {}).get('rich_text', []))
                    if 'Transcript' in toggle_label:
                        try:
                            tr_children = notion_get(f'blocks/{sb["id"]}/children?page_size=30')
                        except Exception:
                            continue
                        lines = []
                        for tb in tr_children.get('results', []):
                            text = block_text(tb).strip()
                            if text:
                                lines.append(text)
                            if len(lines) >= transcript_lines:
                                break
                        transcript_text = ' '.join(lines)

            snips.append({'title': title, 'summary': summary_bullets, 'transcript': transcript_text})
            if len(snips) >= snips_per_episode:
                break

        if snips:
            episodes.append({'episode': episode, 'show': show, 'snips': snips})

    return episodes


# ─── Format for LLM ───────────────────────────────────────────────────────────

def format_readwise(books):
    if not books:
        return ''
    lines = ['=== BOOK HIGHLIGHTS (Readwise) ===',
             'Passages Mauro marked while reading — each highlight is a deliberate selection.', '']
    for book in books:
        lines.append(f'"{book["title"]}" by {book["author"]}')
        for h in book['highlights']:
            lines.append(f'  • {h}')
        lines.append('')
    return '\n'.join(lines)

def format_snipd(episodes):
    if not episodes:
        return ''
    lines = ['=== PODCAST SNIPS (Snipd) ===',
             'Moments Mauro clipped while listening — title + AI summary + transcript excerpt.', '']
    for ep in episodes:
        lines.append(f'Podcast: "{ep["episode"]}" ({ep["show"]})')
        for snip in ep['snips']:
            lines.append(f'  Snip: {snip["title"]}')
            for bullet in snip['summary']:
                lines.append(f'    → {bullet}')
            if snip['transcript']:
                lines.append(f'    Transcript: {snip["transcript"][:400]}')
        lines.append('')
    return '\n'.join(lines)


# ─── OpenAI ───────────────────────────────────────────────────────────────────

def call_openai(prompt):
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        print('ERROR: OPENAI_API_KEY not set', file=sys.stderr)
        return None
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps({'model': 'gpt-4o', 'max_tokens': 1000, 'temperature': 0.3,
                         'messages': [{'role': 'user', 'content': prompt}]}).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {key}'}
    )
    try:
        with _opener().open(req, timeout=30) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'OpenAI error: {e}', file=sys.stderr)
        return None


# ─── Profile ──────────────────────────────────────────────────────────────────

def read_profile():
    if not os.path.exists(USERMD_PATH):
        return '(no profile yet)'
    with open(USERMD_PATH) as f:
        content = f.read()
    sections = []
    for heading in ['Observed Patterns', 'Contradictions & Dilemmas']:
        marker = f'## {heading}'
        if marker in content:
            start = content.index(marker)
            nxt = re.search(r'\n## ', content[start + 3:])
            end = (start + 3 + nxt.start()) if nxt else len(content)
            sections.append(content[start:end].strip())
    return '\n\n'.join(sections) if sections else '(no profile yet)'

def parse_json(raw):
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

def apply(analysis, today, source_tag):
    counts = {'reinforcements': 0, 'contradictions': 0, 'new_candidates': 0}
    for r in analysis.get('reinforcements', []):
        p, ev = r.get('pattern','').strip(), r.get('evidence','').strip()
        if p and ev:
            subprocess.run(['python3', MANAGE, 'add-candidate',
                json.dumps({'text': p, 'evidence': f'[{source_tag}] {ev}',
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
        t, ev = nc.get('text','').strip(), nc.get('evidence','').strip()
        if t and ev:
            subprocess.run(['python3', MANAGE, 'add-candidate',
                json.dumps({'text': t, 'evidence': f'[{source_tag}] {ev}',
                            'source': source_tag, 'date': today})], capture_output=True)
            counts['new_candidates'] += 1
    return counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not os.environ.get('NOTION_API_KEY'):
        print('ERROR: NOTION_API_KEY not set', file=sys.stderr)
        sys.exit(1)

    state = load_state()
    today = str(date.today())

    if too_soon(state, 'lastNotionScan'):
        last = datetime.fromtimestamp(state['lastNotionScan']).strftime('%Y-%m-%d')
        print(f'Skipping: last Notion scan was {last} (min interval: {MIN_INTERVAL}d)')
        return

    print('Fetching Readwise book highlights...')
    books = fetch_readwise(n_books=3, highlights_per_book=6)
    print(f'  {len(books)} books')

    print('Fetching Snipd podcast snips...')
    episodes = fetch_snipd(n_episodes=3, snips_per_episode=4, transcript_lines=6)
    print(f'  {len(episodes)} episodes')

    if not books and not episodes:
        print('No Notion data fetched.')
        return

    profile = read_profile()
    evidence = '\n\n'.join(filter(None, [format_readwise(books), format_snipd(episodes)]))

    prompt = f"""You are building a behavioral profile of Mauro from evidence — not self-descriptions.

The evidence below comes from two intentional selection behaviors:
- Book highlights: passages he chose to mark while reading (Readwise)
- Podcast snips: moments he chose to clip while listening (Snipd)
What a person repeatedly selects reveals what resonates with them — their values, preoccupations, and patterns.

EXISTING PROFILE:
{profile}

EVIDENCE:
{evidence}

Look for:
1. Evidence SUPPORTING an existing pattern (quote the pattern text exactly as in the profile)
2. Evidence CONTRADICTING an existing pattern
3. NEW behavioral signals not in the profile — grounded in observable choices or repeated interests, never self-description

Return ONLY valid JSON, no markdown, no extra text:
{{"reinforcements":[{{"pattern":"...","evidence":"..."}}],
  "contradictions":[{{"a":"...","b":"...","evidence_a":"...","evidence_b":"...","same_domain":true}}],
  "new_candidates":[{{"text":"...","evidence":"..."}}]}}

Be conservative. 0 entries in any list is fine. Do not hallucinate."""

    raw = call_openai(prompt)
    if not raw:
        return

    analysis = parse_json(raw)
    if not analysis:
        return

    counts = apply(analysis, today, 'notion')
    # Reload state to preserve candidates written by manage-profile.py subprocesses
    state = load_state()
    state['lastNotionScan'] = time.time()
    save_state(state)

    print(f'Done: {counts["reinforcements"]} reinforcements, {counts["contradictions"]} contradictions, {counts["new_candidates"]} new candidates')
    if any(counts.values()):
        print('Run `manage-profile.py list` to review candidates.')

if __name__ == '__main__':
    main()
