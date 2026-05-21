---
name: synth
description: On-demand rule-aware Priority Synthesis refresh. Fires the Tier 2 prose layer in rule-aware overlay mode — includes LEARNINGS rules + per-deal context in the prompt so the dashboard pane gets a deeper read against your accumulated principles. Larger LLM payload (~25KB) than the cadenced lean mode (~3.5KB); use when you want the richer synthesis right now, not waiting for the next scheduled fire.
---

# /synth — On-demand rule-aware Priority Synthesis

Fires `synthesize_prose.py --apply --with-rules` synchronously. The dashboard
pane's prose + worthNoticing + clusters + ruleApplications get refreshed with
the current Tier 1 ranking + your full active LEARNINGS rule set as Claude
input.

## When to use this

- You just made a meaningful decision and want the synthesis to factor it in
  before the next scheduled fire (08:30 / 13:30 / 18:00).
- You're heading into a deal call and want the pane's "Worth noticing" line
  + ruleApplications to ground you against your own behavioral principles.
- You want to validate that the dashboard's interpretation matches your gut.

## What it does

```bash
python3 ~/dashboards/routines/compile/synthesize_prose.py --apply --with-rules
```

The `--with-rules` flag swaps the lean prompt (cadenced default) for the
rule-aware prompt at `config/synthesis-prose-prompt-ruleaware.md`. The
script loads `~/dashboards/docs/LEARNINGS-LEDGER.yaml` and feeds the active
rule subset into the Claude call alongside the Tier 1 ranked items.

Output lands in `dashboard-data.json.prioritySynthesis`:
- `.prose` (rule-aware paragraph)
- `.worthNoticing`
- `.clusters[]`
- `.ruleApplications[]` ← NEW in rule-aware mode; mappings of rule→item

## Cost

- $0 marginal under Claude Max (subscription path via `_claude_dispatch`)
- ~6-12s latency (vs 3-8s lean)
- ~25KB Claude payload (vs 3.5KB lean) — bigger but cacheable; the rules
  portion is a stable prefix Anthropic can prompt-cache cheaply

## Graceful fallback

Same as cadenced mode: if Claude Max quota hits or the call fails, the
script exits 0 and the pane keeps showing the previous Tier 2 output
(or Tier 1 only if no prior prose exists). No new state to manage.

## After /synth fires

POST `/warmup` to push the new prose to the dashboard cache:

```bash
curl -s -X POST http://localhost:7777/warmup -o /dev/null -w "%{http_code}\n"
```

Then reload the dashboard tab. The pane will show the refreshed prose +
worthNoticing + clusters + ruleApplications (if any).

## Don't use /synth for

- The first morning glance — the 08:30 cadence already covers that.
- Validating that synthesis exists — that's `/cache-status` or a glance at
  the dashboard.
- Tier 1 (rules) recompute — that's automatic in every cache refresh; if
  you want to force it, just POST `/warmup`.
