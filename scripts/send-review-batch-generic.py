#!/usr/bin/env python3
import json
import subprocess
import sys

MANAGE = '/data/skills/user-profiler/scripts/manage-profile.py'


def load_blocks(kind: str, limit: int):
    cmd = {
        'candidate': ['python3', MANAGE, 'review-batch', str(limit)],
        'reinforcement': ['python3', MANAGE, 'reinforcement-review-batch', str(limit)],
        'contradiction': ['python3', MANAGE, 'contradiction-review-batch', str(limit)],
    }[kind]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    blocks = []
    current = {}
    buttons = []
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                current['buttons'] = buttons
                blocks.append(current)
                current = {}
                buttons = []
            continue
        if line.startswith('Type: '):
            current['type'] = line[6:].strip()
        elif line.startswith('ID: '):
            current['id'] = line[4:].strip()
        elif line.startswith('Text: '):
            current['text'] = line[6:].strip()
        elif line.startswith('Evidence: '):
            current['evidence'] = line[10:].strip()
        elif '-> profile_rate:' in line:
            label, callback = [x.strip() for x in line.split('->', 1)]
            buttons.append({'label': label, 'callback_data': callback})
    if current:
        current['buttons'] = buttons
        blocks.append(current)
    return blocks


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: send-review-batch-generic.py <candidate|reinforcement|contradiction> [n]')
    kind = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    items = load_blocks(kind, limit)
    print(json.dumps({'items': items}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
