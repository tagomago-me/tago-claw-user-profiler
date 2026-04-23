---
name: user-profiler
description: "Build a behavioral profile of the user from observed decisions and public notes, not self-description. Use after substantial conversations to capture behavior signals, and on heartbeat to scan Nostr notes into trait proposals for human review. Promotion to USER.md happens only after repeated evidence and explicit human validation."
---

# User Profiler

Build a behavioral profile from evidence, not self-report.

This skill has two active paths:
1. conversation → candidate signal → human confirmation → promotion to `USER.md`
2. Nostr note → trait proposal/link in DuckDB → human review → possible promotion to `USER.md`

Do not use `USER.md` as evidence source.
`USER.md` is the final consolidated output read by the agent.

---

## Path 1 — conversation signals

Use after a substantial conversation where Mauro revealed something by decision, tradeoff, repetition, or friction.

Good signal types:
- choice under tradeoff
- recurring operating preference
- recurring friction pattern
- contradiction with previously observed behavior

Do not capture:
- self-description without behavioral evidence
- generic topic interests
- small talk

Flow:
1. identify 1-3 signals grounded in action
2. add them with `manage-profile.py add-candidate`
3. review explicitly with Mauro
4. approve or reject
5. only approved patterns go to `USER.md`

Pending conversation candidates live in:
- `/data/memory/profiler-state.json` → `candidates[]`

---

## Path 2 — Nostr trait scan

Run:
```bash
/data/skills/user-profiler/scripts/scan-nostr.py
```

Current behavior:
- reads recent public Nostr notes from the local DuckDB mirror
- skips obvious operational/test noise
- in bootstrap mode, proposes at most 1 new trait per note
- in matching mode, prefers existing validated traits and proposes new ones only when needed
- writes trait proposals and note↔trait links into DuckDB
- records enough evidence metadata in operational state to avoid re-analysis

It does not:
- decide reinforcement vs contradiction against `USER.md`
- write profile patterns directly to `USER.md`
- validate traits by itself

Review and promotion happen later.

---

## Scope

Current production background scan is **Nostr-only**.

Backfill rule:
- do not run backfill headlessly in normal operation
- if older notes are scanned, Mauro must receive review cards for the resulting proposals

---

## Storage model

Canonical data layer:
- `/data/userprofile/`

Operational state:
- `/data/memory/profiler-state.json`
- `/data/.openclaw/user-profiler-review-messages.json`

Final agent-facing output:
- `/data/USER.md`

Do not version runtime data.

---

## Main files

```text
skills/user-profiler/
├── SKILL.md
├── references/
│   └── state-flow.md
└── scripts/
    ├── manage-profile.py
    ├── scan-nostr.py
    ├── send-review-*.py
    ├── trait-review-batch.py
    └── user-profiler-cron.sh
```

---

## Cron behavior

When triggered by the scheduled user-profiler cron, run the Nostr scan path.
The reply should say, briefly and in Portuguese:
1. trait proposals produced
2. note links produced
3. whether the run was skipped by interval
4. or that there were no new candidates today

---

## Read the reference when changing

Read `references/state-flow.md` when changing:
- scanner behavior
- evidence lifecycle
- review pipeline
- storage boundaries
- promotion rules into `USER.md`
