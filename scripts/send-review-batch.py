#!/usr/bin/env python3
import json
import subprocess
import sys

MANAGE = '/data/skills/user-profiler/scripts/manage-profile.py'


def top_candidates(limit=4):
    proc = subprocess.run(
        ['python3', MANAGE, 'review-batch', str(limit)],
        text=True,
        capture_output=True,
        check=True,
    )
    blocks = []
    current = {}
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                blocks.append(current)
                current = {}
            continue
        if line.startswith('ID: '):
            current['id'] = line[4:].strip()
        elif line.startswith('Text: '):
            current['text'] = line[6:].strip()
        elif line.startswith('Evidence: '):
            current['evidence'] = line[10:].strip()
    if current:
        blocks.append(current)
    return blocks


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    items = top_candidates(limit)
    print(json.dumps({'items': items}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
