# User Profiler state flow

## Objetivo

Descrever a arquitetura atual do user-profiler:
- onde os dados canônicos vivem
- onde o estado operacional vive
- como a evidência circula
- como algo chega até `USER.md`

---

## Modelo em uma linha

O user-profiler coleta evidência, transforma parte dela em propostas estruturadas, submete isso a revisão humana e só então promove saídas consolidadas para `USER.md`.

A regra central é simples:
- evidência não nasce em `USER.md`
- `USER.md` só recebe resultado consolidado

---

## Storage boundaries

### Camada canônica

Fica em:
- `/data/userprofile/`

Inclui:
- raw append-only stores
- DuckDB (`userprofile.duckdb`)
- tabelas de traits
- tabelas de links trait↔evidence
- feedback persistido

### Estado operacional

Fica em:
- `/data/memory/profiler-state.json`
- `/data/.openclaw/user-profiler-review-messages.json`

Serve para:
- fila de candidatos de conversa
- ledger leve de evidência analisada
- timestamps e cursores de execução
- mapeamento de mensagens de review

### Saída consolidada

Fica em:
- `/data/USER.md`

Função:
- ser o resumo final lido pelo agente
- não ser a origem canônica da evidência
- não alimentar inferência como se fosse evidência nova

---

## Path A — conversation → USER.md

Script principal:
- `/data/skills/user-profiler/scripts/manage-profile.py`

Fluxo:
1. o agente observa um sinal comportamental numa conversa relevante
2. grava um candidate em `profiler-state.json`
3. Mauro aprova ou rejeita
4. quando o padrão é aprovado para promoção, ele é escrito em `USER.md`

Unidade principal aqui:
- `candidate`

Estado mínimo envolvido:
- `candidates[]`
- feedback associado

---

## Path B — Nostr → trait proposal

Script principal:
- `/data/skills/user-profiler/scripts/scan-nostr.py`

Entradas:
- notas públicas do espelho local em DuckDB
- pubkey do Mauro via env
- `profiler-state.json` para controle operacional

Fluxo:
1. lê notas recentes
2. filtra ruído operacional
3. propõe trait nova ou reutiliza trait validada
4. grava trait e link nota↔trait em DuckDB
5. marca evidência como analisada no state
6. revisão humana posterior decide o que pode subir para saída consolidada

Unidades principais aqui:
- `trait`
- `profile_trait_note_link`
- `evidence`

Importante:
- esse fluxo não escreve direto em `USER.md`
- esse fluxo não usa `USER.md` como base de evidência

---

## Evidence model

Cada entrada em `state["evidence"]` representa uma unidade estável de evidência já processada operacionalmente.

Campos típicos:
- `source`
- `source_id`
- `created_at`
- `content_hash`
- `preview` / `quote` / `content_excerpt`
- `analyzed_at`
- `analysis_version`
- `trait_scan_no_signal_at`
- `trait_scan_no_signal_version`

Regra de ID:
- a mesma evidência precisa gerar o mesmo ID sempre
- para Nostr: `nostr:<event.id>`

---

## Re-analysis control

No scan de Nostr, a reanálise é evitada com:
- `ANALYSIS_VERSION`
- `already_analyzed(...)`
- `mark_analyzed(...)`

Uma nota é pulada quando:
- já está registrada no ledger de evidência
- foi analisada na versão atual
- e já existe outcome suficiente no DuckDB

Se a heurística mudar de forma relevante:
- sobe `ANALYSIS_VERSION`
- a evidência volta a ficar elegível

---

## Review pipeline

O scan produz proposta.
A revisão decide validade.
A promoção decide o que entra em `USER.md`.

Peças principais:
- `send-review-batch.py`
- `send-telegram-review-batch.py`
- `trait-review-batch.py`
- `bootstrap-trait-cards.py`
- `manage-profile.py`

---

## Promotion rule

Regra estrutural:
- evidência nasce fora de `USER.md`
- proposta é revisada por humano
- só o resultado consolidado vai para `USER.md`

`USER.md` pode ser destino final de leitura do agente sem ser origem canônica.

Se houver conflito entre `USER.md` e a camada canônica em `/data/userprofile/`, corrija `USER.md`.

---

## Scope

Fluxo de produção ativo hoje:
- Nostr
- sinais observados em conversa

Fora do fluxo principal atual:
- scans pesados de Blog
- scans pesados de Notion

Se voltarem, entram por uma interface explícita, não por acoplamento implícito à arquitetura antiga.
