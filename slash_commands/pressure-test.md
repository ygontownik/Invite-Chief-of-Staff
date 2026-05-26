---
description: Pressure-test a high-stakes action against accumulated context. Queries entity knowledge graph + Practice Patterns + DECISIONS.md before you commit. Use before sending an LP email, signing an NDA, committing to a term, or any action you can't undo cleanly.
argument-hint: "<action description>"
---

# /pressure-test (deprecated alias for /jane challenge)

**As of 2026-05-26, /pressure-test is a shim that delegates to /jane challenge.**

The full subcommand set (including challenge) is documented in /jane.

This shim will be removed on 2026-06-25 (30-day deprecation window).

## Procedure

1. Take `$ARGUMENTS` as the topic.
2. Invoke `/jane challenge $ARGUMENTS` (i.e. dispatch to the jane skill
   with subcommand=challenge and the same arguments).
3. Render the output.

Migration: replace `/pressure-test <topic>` with `/jane challenge <topic>`.
