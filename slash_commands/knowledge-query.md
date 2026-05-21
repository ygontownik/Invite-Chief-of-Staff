---
name: knowledge-query
description: Query the persistent intelligence knowledge base — all research ever indexed from the daily briefing sources — and synthesize an answer in-session. No API calls.
---

## Usage

Invoke as: `/knowledge-query <question>`

Optional qualifiers the user may include:
- `--deal <deal_id>` — scope results to articles already flagged as relevant to that deal
- `--source <tracker_key>` — limit to one source (e.g. "GS — Macro Market")
- `--since YYYY-MM-DD` — only articles from that date forward
- `--top-k N` — number of chunks to retrieve (default 10)

Examples:
- `/knowledge-query what do we know about PJM interconnection queue delays?`
- `/knowledge-query ERCOT land valuations --deal cholla`
- `/knowledge-query LNG FID projects --since 2026-01-01`
- `/knowledge-query data center water restrictions --top-k 15`

---

## Steps

### 1. Parse the query

Extract from the user's message:
- `QUESTION` — the natural language question (everything before any `--` flags)
- `DEAL` — value of `--deal` if present (else empty)
- `SOURCE` — value of `--source` if present (else empty)
- `SINCE` — value of `--since` if present (else empty)
- `TOPK` — value of `--top-k` if present (default: 10)

### 2. Run the query

```bash
DATE=$(python3 -c "from datetime import date; print(date.today())")
python3 ~/cos-pipeline/tools/knowledge_query.py \
    "${QUESTION}" \
    ${DEAL:+--deal "$DEAL"} \
    ${SOURCE:+--source "$SOURCE"} \
    ${SINCE:+--since "$SINCE"} \
    --top-k ${TOPK:-10} \
    --format text
```

Read the output carefully. Each chunk is formatted as:
```
[Source | Date | sim=0.NN]
Title: <article title>
<excerpt>
Doc: <Google Drive URL>
```

### 3. Synthesize in-session

Using ONLY the retrieved chunks as your source material (do not invent facts):

- **Lead with the answer** — what does the research say about the question? One to two sentences.
- **For each material point**, cite `[Source | Date]` inline, immediately after the claim.
- **Source-balance rule**: cite no more than 3 claims per source.
- If retrieved chunks are sparse or low-similarity (<0.50), say so explicitly rather than padding.
- If the index appears stale (⚠ warning printed), note that to the user.

### 4. Deal-intel emission (if --deal was specified)

If `--deal <deal_id>` was used AND the synthesis surfaced non-trivial new information about that deal, emit a `---DEAL-INTEL---` block so `intel_capture.py scan-claude-code` routes it to the deal's log:

```
---DEAL-INTEL---
deal: <deal_id>
date: <today>
title: Knowledge query: <brief title of what was learned>
summary: <1-2 sentences>
facts:
  - <fact 1, with numbers + named entities>
  - <fact 2>
actions: []
---END-DEAL-INTEL---
```

Only emit if the content is genuinely new or materially useful — not for every query.

### 5. Offer follow-up

After the synthesis, offer one of:
- A refined query with a narrower scope
- Running the same query `--since` a more recent date
- Filtering `--deal <id>` if the answer is relevant to an active deal
