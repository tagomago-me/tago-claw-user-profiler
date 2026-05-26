---
name: user-profiler
description: "Build a behavioral profile of the user by observing decisions and Nostr notes — not self-descriptions. Use after a substantial conversation to extract behavioral signals, or on heartbeat to scan Nostr notes for patterns and contradictions. Writes to USER.md only after 4-5 independent signals confirm a pattern. Records contradictions and dilemmas as-is, without resolving them."
---

# User Profiler

Builds a behavioral profile from evidence, not self-report.
Two distinct jobs — do not mix them.

---

## Job 1 — Post-Conversation Probe

**When:** At the end of a substantial conversation where Mauro made real decisions,
expressed preferences, or revealed something by what he chose to do or not do.
Not after small talk or simple task requests.

**What to look for:**
- Decisions (especially when something was chosen over something else)
- Patterns of what he returns to vs what he drops
- What causes friction vs what flows easily
- Contradictions with things already in USER.md

**How to do it (agent runs this internally):**

1. Review the conversation just had. Identify 1-3 candidate behavioral signals.
   A signal must be grounded in an action or decision, not a self-description.
   Example signal: "Chose to go slower and understand each step rather than get a fast result"
   Bad signal: "Said he cares about quality"

2. For each candidate, call:
   ```bash
   /data/skills/user-profiler/scripts/manage-profile.py add-candidate \
     '{"text": "...", "evidence": "brief quote or description of the decision", "source": "conversation", "date": "YYYY-MM-DD"}'
   ```

3. Show Mauro the candidates in plain language. One at a time. For each:
   - State the observation
   - State the evidence it's based on
   - Ask: "Does this feel accurate?"
   - If yes → `manage-profile.py approve <id>`
   - If no → `manage-profile.py reject <id>`

4. After approval, tell him the signal count for that pattern:
   - If < 4: "Noted. Need more signals before it goes into your profile."
   - If ≥ 4: "This is now in your profile."

**Never write to USER.md without approval.**

---

## Job 2 — Nostr Scan (Heartbeat, twice/week)

**When:** Triggered by heartbeat. Run only if `lastNostrScan` in state is > 3 days ago.

```bash
/data/skills/user-profiler/scripts/scan-nostr.py
```

- Pulls 5 recent public Nostr notes from Mauro's relays
- Analyzes with GPT-4o against existing profile
- No confirmation needed — background work

---

## Job 3 — Blog Scan (Heartbeat, weekly)

**When:** Triggered by heartbeat. Run only if `lastBlogScan` in state is > 7 days ago.

```bash
/data/skills/user-profiler/scripts/scan-blog.py
```

- Pulls the 5 most recent posts from two RSS feeds:
  - `tagomago.me/feed/` — main blog, mostly English
  - `tagomago.me/category/visao/feed/` — Portuguese posts (migrated from VQEB/Closte)
- Blog posts are treated as **curated, intentional signals** — heavier weight than Nostr notes
- Analyzes with GPT-4o against existing profile
- No confirmation needed — background work

---

## Job 4 — Notion Scan (Heartbeat, twice/week, offset from Nostr)

**When:** Triggered by heartbeat. Run only if `lastNotionScan` in state is > 3 days ago.

```bash
/data/skills/user-profiler/scripts/scan-notion.py
```

- Pulls highlights from the 3 most recently read **books** (Readwise Library)
- Pulls snips from the 3 most recently clipped **podcast episodes** (Snipd)
  - Includes: snip title + AI summary bullets + transcript excerpt per snip
- Analyzes with GPT-4o against existing profile
- No confirmation needed — background work

**Databases:**
- Readwise Library: `1ea2c1e8779c8137b03fe00b8b94392e` (Category = Books)
- Snipd: `1ea2c1e8779c80c48030ddd64a701758`

---

All background scans report briefly at next conversation: "Nostr scan: N reinforcements, N contradictions, N candidates." Same format for blog and Notion scans.

---

## USER.md Sections (managed by this skill)

The skill appends and maintains two sections in USER.md:

### Observed Patterns
Behavioral patterns confirmed by 4+ independent signals.
Pending patterns (< 4 signals) are in `/data/memory/profiler-candidates.json`.

Format:
```
## Observed Patterns

- **[pattern statement]** — *N signals* — last: YYYY-MM-DD
  - [source type]: [brief evidence]
  - [source type]: [brief evidence]
```

### Contradictions & Dilemmas
Tensions between observed behaviors. Not problems to resolve — patterns worth sitting with.
Includes domain context and whether A and B are from the same life area.

Format:
```
## Contradictions & Dilemmas

- **[A] ↔ [B]** — first: YYYY-MM-DD — same domain: yes/no
  - A: [evidence for A]
  - B: [evidence for B]
```

---

## Scripts

```
skills/user-profiler/
├── SKILL.md                      ← this file
└── scripts/
    ├── manage-profile.py         ← CRUD: candidates, patterns, contradictions in USER.md
    ├── run-background-scans.py   ← Ordered background scan runner used by cron
    ├── scan-nostr.py             ← Nostr scan (heartbeat job, state key: lastNostrScan)
    ├── scan-blog.py              ← Blog scan: tagomago.me EN + PT/VQEB (heartbeat job, state key: lastBlogScan)
    └── scan-notion.py            ← Readwise + Snipd scan (heartbeat job, state key: lastNotionScan)
```

## State file

`/data/memory/profiler-state.json` — tracks candidates and last scan timestamp.
Created automatically on first run.
