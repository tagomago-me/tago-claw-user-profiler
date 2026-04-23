#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

SEND_BATCH = Path('/data/skills/user-profiler/scripts/send-review-batch-generic.py')
MANAGE = Path('/data/skills/user-profiler/scripts/manage-profile.py')
PUBLISH_GATE_ENV = Path('/data/.openclaw/publish-gate.env')
OPENCLAW_CONFIG = Path('/data/.openclaw/openclaw.json')


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def truncate(text: str, limit: int) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + '…'


def load_items(kind: str, limit: int) -> list[dict]:
    proc = run(['python3', str(SEND_BATCH), kind, str(limit)])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or 'failed to load review batch')
    return list((json.loads(proc.stdout)).get('items') or [])


def load_token() -> str:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip() or os.environ.get('TELEGRAM_APPROVAL_BOT_TOKEN', '').strip()
    if token:
        return token
    if PUBLISH_GATE_ENV.exists():
        for line in PUBLISH_GATE_ENV.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                if k.strip() in {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_APPROVAL_BOT_TOKEN'} and v.strip():
                    return v.strip()
    if OPENCLAW_CONFIG.exists():
        obj = json.loads(OPENCLAW_CONFIG.read_text(encoding='utf-8'))
        token = ((((obj.get('channels') or {}).get('telegram') or {}).get('botToken')) or '').strip()
        if token:
            return token
    raise RuntimeError('telegram bot token not found')


def tg_post(token: str, method: str, body: dict) -> dict:
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode('utf-8', errors='replace')) from e
    if not raw.get('ok'):
        raise RuntimeError(str(raw))
    return raw['result']


def button_style(label: str) -> str:
    if label.startswith('must') or label.startswith('valid'):
        return 'success'
    if label.startswith('nice'):
        return 'primary'
    if label.startswith('review'):
        return 'secondary'
    return 'danger'


def send_card(token: str, target: str, item: dict, silent: bool = False) -> dict:
    iid = str(item['id'])
    kind = str(item['type'])
    body = f"[{kind}] `{iid}`\n{item.get('text','').strip()}\n\nEvidence: {truncate(item.get('evidence','').strip(), 700)}"
    keyboard = [[{'text': b['label'], 'callback_data': b['callback_data']} for b in item.get('buttons', [])]]
    payload = {'chat_id': int(target) if str(target).lstrip('-').isdigit() else str(target), 'text': body[:4096], 'reply_markup': {'inline_keyboard': keyboard}}
    if silent:
        payload['disable_notification'] = True
    result = tg_post(token, 'sendMessage', payload)
    mid = result.get('message_id')
    if mid is not None:
        run(['python3', str(MANAGE), 'review-register', f'{kind}:{iid}', str(mid)])
    return {'type': kind, 'id': iid, 'message_id': mid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['candidate', 'reinforcement', 'contradiction'])
    ap.add_argument('--target', required=True)
    ap.add_argument('--limit', type=int, default=4)
    ap.add_argument('--silent', action='store_true')
    args = ap.parse_args()
    items = load_items(args.kind, args.limit)
    if not items:
        print(json.dumps({'ok': True, 'sent': 0, 'items': []}, ensure_ascii=False))
        return 0
    token = load_token()
    sent = [send_card(token, args.target, item, silent=args.silent) for item in items]
    print(json.dumps({'ok': True, 'sent': len(sent), 'items': sent}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
