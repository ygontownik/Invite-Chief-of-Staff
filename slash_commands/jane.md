---
name: jane
description: Invoke Jane — read current strategic actions + suggestions, or trigger a refresh. Subcommands: refresh [quick|deep], <slug>, <path>.
---

# /jane — Jane on-demand

Reads current Jane output from dashboard-data.json prioritySynthesis and
displays. Or triggers a refresh (Sonnet for quick, Opus for deep).

## Usage

- `/jane` — show all current janeStrategicActions + janeSuggestions
- `/jane refresh` — run critique_gaps.py (Sonnet hygiene) immediately
- `/jane refresh quick` — explicit quick refresh
- `/jane refresh deep` — run critique_strategic.py (Opus portfolio) — heavier; use sparingly
- `/jane <slug>` — filter output to a specific deal (e.g., `/jane fit`)
- `/jane <path>` — filter to a specific career path (e.g., `/jane drw`, `/jane align`)

## Procedure

1. Parse the argument from $ARGUMENTS:
   - No args → display mode (show all)
   - "refresh" + optional "quick"|"deep" → trigger appropriate critic
   - matches a deal slug from deal-system-data.json → filter mode for that deal
   - matches a path name ("tomac_cove", "drw", "align") → filter mode for that path

2. **Display mode** (no args or filter):
   - Read `~/dashboards/data/compiled/dashboard-data.json`
   - Extract `prioritySynthesis.janeStrategicActions` and `.janeSuggestions`
   - If filter argument provided, narrow to entries whose `deal_or_path`
     (strategic) or `deal` (suggestions) matches
   - Render as a compact two-section view: strategic actions first, then
     hygiene suggestions
   - Cite generated_at timestamps (note staleness if >4h)

3. **Refresh mode**:
   - `quick` (default) → `python3 ~/dashboards/routines/compile/critique_gaps.py --apply`
   - `deep` → `python3 ~/dashboards/routines/compile/critique_strategic.py --apply`
   - Report success / quota error / non-fatal error
   - After refresh, automatically run display mode to show the fresh output

## Display format

For strategic actions, render each surfaced item as:

```
STRATEGIC ACTIONS  (generated <timestamp>, <age>)
─────────────────────────────────────────────────
[<deal_or_path>]  verdict: <elevate|matches_curated>
  Action: <jane_strategic_action>
  Why: <rationale>
  Evidence: <evidence list>
  Cross-deal: <cross_deal_link or none>
```

For hygiene suggestions, render each surfaced item as:

```
JANE SUGGESTS  (generated <timestamp>, <age>)
─────────────────────────────────────────────
[<deal>]  kind: <kind>  severity: <N>
  <rationale>
  Evidence: <evidence>
  Suggested: <suggested_action>
```

If both sections are empty, say: "Jane has no output yet. Run `/jane refresh` to populate,
or wait for the next scheduled warmup."

## Notes

- Jane's substrate is Drive-canonical (north_star + decision_state_jane +
  jane_brief Drive Docs). On a deep refresh, critique_strategic.py calls
  `_ensure_fresh_mirrors()` first to pull any out-of-date Drive Docs to
  local before reading.

- If critique_gaps.py or critique_strategic.py doesn't exist yet (Tasks C
  and C2 of Phase 1 critic build), `/jane refresh` will report "critic
  script not found yet" — display mode still works once gap_detector is
  wired (Task A).

- The 9 active deal slugs: align_infra, bbeh, cholla, fit, gridfree, pfs,
  pngts, thunderhead, unitil. Career paths: tomac_cove, drw, align.

- Phase 2 will extend this skill to conversational queries ("/jane what's
  stuck on cholla", "/jane who haven't I talked to in 30d") — out of scope
  for v1.

## Error handling

- If dashboard-data.json is missing: say so and suggest running `/check-system`.
- If critique script doesn't exist: print "critic script not found yet — Task C/C2
  of the Jane critic build hasn't shipped. Display mode still reads any existing
  output."
- If the script fails with a quota error: say "Claude quota hit — try again later."
- All errors are non-fatal. Display mode always runs after any refresh attempt.
