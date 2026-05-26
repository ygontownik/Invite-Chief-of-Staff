---
name: knowledge-query
description: Query the persistent intelligence knowledge base — all research ever indexed from the daily briefing sources — and synthesize an answer in-session. No API calls.
---

# /knowledge-query (deprecated alias for /jane ask)

**As of 2026-05-26, /knowledge-query is a shim that delegates to /jane ask.**

Removed on 2026-06-25 (30-day deprecation window).

## Procedure

1. Take `$ARGUMENTS` as the question.
2. Invoke `/jane ask $ARGUMENTS`.
3. Render the output.

Migration: replace `/knowledge-query <question>` with `/jane ask <question>`.
