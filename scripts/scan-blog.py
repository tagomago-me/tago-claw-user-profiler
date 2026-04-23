#!/usr/bin/env python3
"""
Scan tagomago.me blog posts for behavioral signals (user-profiler heartbeat job).

Sources:
  - tagomago.me/feed/ (new blog, mostly English)
  - tagomago.me/pt/feed/ (VQEB legacy blog, mostly Portuguese — migrating from Closte)

Fetches the 5 most recent posts from each feed, reads USER.md, then uses GPT-4o
to find reinforcements, contradictions, and new candidate behavioral signals.

Requires: OPENAI_API_KEY in environment.
State: /data/memory/profiler-state.json  (key: lastBlogScan)
"""

import sys
import os
import re
import json
import ssl
import urllib.request
import time
import hashlib
import subprocess
from datetime import date, datetime
from html.parser import HTMLParser

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

USERMD_PATH  = '/data/USER.md'
STATE_PATH   = '/data/memory/profiler-state.json'
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MANAGE       = os.path.join(SCRIPT_DIR, 'manage-profile.py')
MIN_INTERVAL = 7   # days — blogs update slower than Nostr
ANALYSIS_VERSION = '2026-04-21-c'

FEEDS = [
    ('tagomago-en', 'https://tagomago.me/feed/'),
    ('tagomago-pt', 'https://tagomago.me/category/visao/feed/'),  # migrated VQEB content
]
POSTS_PER_FEED = 5

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


# ─── State ────────────────────────────────────────────────────────────────────

def load_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)
    state.setdefault('evidence', {})
    return state

def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)
    try:
        subprocess.run(['python3', '/data/userprofile/scripts/sync_profiler_state.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        pass

def too_soon(state, key='lastBlogScan'):
    if os.environ.get('USER_PROFILER_FORCE_DAILY', '').strip() in {'1', 'true', 'yes', 'on'}:
        return False
    last = state.get(key)
    return bool(last) and (time.time() - last) / 86400 < MIN_INTERVAL


# ─── RSS Fetch ────────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []

    def handle_data(self, d):
        self.result.append(d)

    def get_text(self):
        return ' '.join(self.result)

def strip_html(html):
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text().strip()

def _tag_text(xml, tag):
    """Extract first occurrence of <tag>…</tag> (no namespace)."""
    m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', xml, re.DOTALL)
    return m.group(1).strip() if m else ''

def _cdata(text):
    """Strip CDATA wrappers."""
    m = re.match(r'<!\[CDATA\[(.*?)\]\]>', text, re.DOTALL)
    return m.group(1).strip() if m else text

def fetch_feed(url, count=5):
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (profiler-bot)'})
    try:
        with opener.open(req, timeout=20) as r:
            xml = r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'Feed fetch error ({url}): {e}', file=sys.stderr)
        return []

    items = re.split(r'<item[^>]*>', xml)[1:]  # skip channel header
    posts = []
    for item in items[:count]:
        title   = strip_html(_cdata(_tag_text(item, 'title')))
        link    = _cdata(_tag_text(item, 'link')) or _cdata(_tag_text(item, 'guid'))
        pub     = _tag_text(item, 'pubDate')
        content = _cdata(_tag_text(item, 'content:encoded') or _tag_text(item, 'description'))
        body    = strip_html(content)[:1500]  # cap at 1500 chars
        if title:
            posts.append({'title': title, 'link': link, 'date': pub, 'body': body})
    return posts


def make_blog_id(post):
    link = (post.get('link') or '').strip()
    title = (post.get('title') or '').strip()
    pub = (post.get('date') or '').strip()
    feed = (post.get('feed') or '').strip()
    canonical = link or f'{feed}|{title}|{pub}'
    digest = hashlib.sha1(canonical.encode('utf-8')).hexdigest()[:16]
    return f'blog:{feed}:{digest}'


# ─── OpenAI ───────────────────────────────────────────────────────────────────

def call_openai(prompt):
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        print('ERROR: OPENAI_API_KEY not set', file=sys.stderr)
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps({'model': 'gpt-4o', 'max_tokens': 900, 'temperature': 0.3,
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

def ensure_evidence_record(state, evidence_id, payload):
    bucket = state.setdefault('evidence', {})
    rec = bucket.get(evidence_id, {})
    rec.update({k: v for k, v in payload.items() if v is not None})
    bucket[evidence_id] = rec
    return rec

def already_analyzed(state, evidence_id):
    rec = state.get('evidence', {}).get(evidence_id, {})
    return bool(rec.get('analyzed_at') and rec.get('analysis_version') == ANALYSIS_VERSION)

def mark_analyzed(state, evidence_ids):
    now = time.time()
    for evidence_id in evidence_ids:
        rec = state.setdefault('evidence', {}).setdefault(evidence_id, {})
        rec['analyzed_at'] = now
        rec['analysis_version'] = ANALYSIS_VERSION


def record_reinforcement(state, source_tag, pattern, evidence, today):
    bucket = state.setdefault('reinforcements', [])
    entry = {
        'source': source_tag,
        'pattern': pattern,
        'evidence': evidence,
        'date': today,
    }
    for existing in bucket:
        if (
            existing.get('source') == entry['source']
            and existing.get('pattern') == entry['pattern']
            and existing.get('evidence') == entry['evidence']
        ):
            return False
    bucket.append(entry)
    return True

def candidate_allowed(text, evidence):
    t = (text or '').strip()
    tl = t.lower()
    ev = (evidence or '').strip()
    if not t or not ev:
        return False
    if len(t.split()) < 6:
        return False
    if any(tl.startswith(prefix) for prefix in GENERIC_PREFIXES):
        return False
    banned = ('technology', 'innovation', 'storytelling', 'human culture', 'education methods', 'science and entrepreneurship')
    if any(term in tl for term in banned) and 'because' not in tl and 'by ' not in tl and 'when ' not in tl:
        return False
    return True

def apply(analysis, today, source_tag):
    import subprocess
    state = load_state()
    counts = {'reinforcements': 0, 'contradictions': 0, 'new_candidates': 0}
    for r in analysis.get('reinforcements', []):
        pattern, evidence = r.get('pattern', '').strip(), r.get('evidence', '').strip()
        if pattern and evidence:
            if record_reinforcement(state, source_tag, pattern, evidence, today):
                counts['reinforcements'] += 1
    for c in analysis.get('contradictions', []):
        a, b = c.get('a', '').strip(), c.get('b', '').strip()
        if a and b:
            subprocess.run(['python3', MANAGE, 'add-contradiction',
                json.dumps({'a': a, 'b': b, 'evidence_a': c.get('evidence_a', ''),
                            'evidence_b': c.get('evidence_b', ''),
                            'same_domain': c.get('same_domain', None), 'date': today})], capture_output=True)
            counts['contradictions'] += 1
    for nc in analysis.get('new_candidates', []):
        text = nc.get('text', '').strip()
        analysis_note = nc.get('analysis', '').strip()
        if candidate_allowed(text, analysis_note):
            proc = subprocess.run(['python3', MANAGE, 'add-candidate',
                json.dumps({'text': text,
                            'analysis': analysis_note,
                            'source': source_tag, 'date': today,
                            'evidence_ids': nc.get('evidence_ids', [])})], capture_output=True, text=True)
            out = (proc.stdout or '') + '\n' + (proc.stderr or '')
            if proc.returncode == 0 and ('Candidate added:' in out or 'Signal added to existing candidate' in out):
                counts['new_candidates'] += 1
    save_state(state)
    return counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    today = str(date.today())

    if too_soon(state, 'lastBlogScan'):
        last = datetime.fromtimestamp(state['lastBlogScan']).strftime('%Y-%m-%d')
        print(f'Skipping: last blog scan was {last} (min interval: {MIN_INTERVAL}d)')
        return

    all_posts = []
    pending_posts = []
    pending_ids = []
    for feed_tag, feed_url in FEEDS:
        posts = fetch_feed(feed_url, count=POSTS_PER_FEED)
        print(f'Fetched {len(posts)} posts from {feed_tag}')
        for p in posts:
            p['feed'] = feed_tag
            all_posts.append(p)
            stable = p.get('link') or f'{feed_tag}:{p.get("title","")}:{p.get("date","")}'
            blog_id = make_blog_id(p)
            evidence_id = blog_id
            body = (p.get('body') or '').strip()
            ensure_evidence_record(state, evidence_id, {
                'source': 'blog',
                'blog_id': blog_id,
                'feed': feed_tag,
                'source_id': stable,
                'title': p.get('title'),
                'published_at': p.get('date'),
                'link': p.get('link'),
                'content_hash': hashlib.sha1(body.encode('utf-8')).hexdigest() if body else None,
                'preview': body[:280],
                'quote': body[:500],
                'body_excerpt': body[:500],
            })
            if already_analyzed(state, evidence_id):
                continue
            p['_evidence_id'] = evidence_id
            pending_posts.append(p)
            pending_ids.append(evidence_id)

    if not all_posts:
        print('No blog posts fetched.')
        return
    if not pending_posts:
        state['lastBlogScan'] = time.time()
        save_state(state)
        print('No unanalyzed blog posts.')
        return

    save_state(state)

    profile = read_profile()
    posts_text = '\n\n---\n\n'.join(
        f'EVIDENCE_ID: {p.get("_evidence_id")}\n[{p["feed"]}] {p["title"]} ({p["date"]})\n{p["body"]}'
        for p in pending_posts
    )

    prompt = f"""You are building a behavioral profile of Mauro from evidence — not self-descriptions.

These are recent posts from his public blogs (tagomago.me). The English blog is newer and more curated; the Portuguese blog (tagomago-pt) is a legacy science communication blog being migrated. Both reveal what he chooses to write about, how he frames problems, and what he cares about enough to publish.

BLOG POSTS:
{posts_text}

EXISTING PROFILE:
{profile}

Look for:
1. Evidence SUPPORTING an existing pattern (quote the pattern exactly)
2. Evidence CONTRADICTING an existing pattern
3. NEW behavioral signals only when they are behaviorally specific and grounded in topic choice, framing habit, recurring argument pattern, or persistent tension. Never output broad summaries of subject matter.

Key distinction: blog posts are edited and intentional, unlike Nostr notes. Weight them accordingly — they reflect what he thinks is worth sharing publicly, not spontaneous reactions.

Rules for new_candidates:
- Good: "Mauro attacks institutional norms by reframing them as control systems.", "Mauro uses public writing to challenge mainstream assumptions.", "Mauro returns to learning-through-play as a serious educational stance."
- Bad: "Mauro is interested in education.", "Mauro values storytelling.", "Mauro has a background in science and entrepreneurship."
- Prefer patterns visible in how he frames and argues, not just what topic the post mentions.
- Self-descriptions in bio/about pages are weak evidence. Ignore them unless strongly reinforced elsewhere.
- High precision over recall. 0 new candidates is better than a vague one.

Return ONLY valid JSON, no markdown:
{{"reinforcements":[{{"pattern":"...","evidence":"..."}}],
  "contradictions":[{{"a":"...","b":"...","evidence_a":"...","evidence_b":"...","same_domain":true}}],
  "new_candidates":[{{"text":"...","analysis":"short internal rationale, not a quote","evidence_ids":["blog:..."]}}]}}

For any returned item, include the exact supporting evidence ids from the EVIDENCE_ID labels when available.
For new_candidates, do not paraphrase the source as evidence text. Put your inference in `analysis`, and rely on `evidence_ids` for the primary quote.

Be conservative. 0 entries is fine. No hallucination."""

    raw = call_openai(prompt)
    if not raw:
        return

    analysis = parse_json_response(raw)
    if not analysis:
        return

    counts = apply(analysis, today, 'blog')
    state = load_state()
    mark_analyzed(state, pending_ids)
    state['lastBlogScan'] = time.time()
    save_state(state)

    print(f'Done: {counts["reinforcements"]} reinforcements, {counts["contradictions"]} contradictions, {counts["new_candidates"]} new candidates')

if __name__ == '__main__':
    main()
