#!/usr/bin/env python3
import json
import subprocess
import sys

MANAGE = '/data/skills/user-profiler/scripts/manage-profile.py'


def get_candidate(cid):
    proc = subprocess.run(['python3', MANAGE, 'review-batch', '50'], text=True, capture_output=True, check=True)
    current = {}
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current.get('id') == cid:
                return current
            current = {}
            continue
        if line.startswith('ID: '):
            current['id'] = line[4:].strip()
        elif line.startswith('Text: '):
            current['text'] = line[6:].strip()
        elif line.startswith('Evidence: '):
            current['evidence'] = line[10:].strip()
    if current.get('id') == cid:
        return current
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: send-review-card.py <candidate-id>')
    cid = sys.argv[1]
    c = get_candidate(cid)
    if not c:
        raise SystemExit(f'candidate not found: {cid}')
    print(json.dumps(c, ensure_ascii=False))


if __name__ == '__main__':
    main()
