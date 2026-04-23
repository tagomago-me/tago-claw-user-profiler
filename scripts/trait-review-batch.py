#!/usr/bin/env python3
from __future__ import annotations

import json
import duckdb

DB_PATH = '/data/userprofile/userprofile.duckdb'


def main() -> int:
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute(
        """
        select
          t.trait_id,
          t.trait_text,
          t.proposal_count,
          l.event_id,
          coalesce(l.evidence_excerpt, ''),
          coalesce(l.llm_confidence, 'medium')
        from raw.profile_traits t
        left join raw.profile_trait_note_links l on l.trait_id = t.trait_id
        where coalesce(t.status, 'proposed') = 'proposed'
        order by t.proposal_count desc, t.last_seen_at desc, l.imported_at desc
        limit 20
        """
    ).fetchall()
    items = []
    seen = set()
    for trait_id, trait_text, proposal_count, event_id, evidence_excerpt, llm_confidence in rows:
        if trait_id in seen:
            continue
        seen.add(trait_id)
        items.append({
            'trait_id': trait_id,
            'trait_text': trait_text,
            'proposal_count': proposal_count,
            'event_id': event_id,
            'evidence_excerpt': evidence_excerpt,
            'llm_confidence': llm_confidence,
            'review_callback': f'trait_rate:{trait_id}:review',
            'valid_callback': f'trait_rate:{trait_id}:valid',
            'weak_callback': f'trait_rate:{trait_id}:weak',
            'not_callback': f'trait_rate:{trait_id}:not',
        })
    print(json.dumps({'items': items}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
