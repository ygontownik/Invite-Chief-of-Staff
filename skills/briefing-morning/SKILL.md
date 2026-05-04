---
name: briefing-morning
description: Daily morning market briefing produced by Claude Code (claude -p) instead of NotebookLM. Domain-aware via firm_context.yaml :: domain. Tuesday–Friday email; Mon/Sat/Sun skipped. Replaces notebooklm-daily-briefing if Track G validates.
status: SPIKE — not wired to LaunchAgent; runs only via run_spike.py until Track G cut-over decision is made.
---

You are running the COS morning briefing in CLAUDE-CODE mode. This is the Track G replacement candidate for `notebooklm-daily-briefing`. Until Track G recommends CUT_OVER, both SKILLs may run in parallel for A/B comparison; only the legacy NotebookLM SKILL writes to the master Doc and sends email by default.

The harness (`run_spike.py`) does the heavy lifting. Your job is to invoke it correctly, gate on output quality, and write/email per the rules below.

## INPUTS

- **Domain:** read from `~/cos-pipeline/firm_context.yaml :: domain`. Default `infra-pe` if unset.
- **Prompt template:** `~/cos-pipeline/domains/<domain>/prompts/briefing-morning.txt` (must exist; abort if missing).
- **Source docs:** same set as `notebooklm-daily-briefing` baseline — pulled directly from Drive (NOT through the NotebookLM browser auto-sync). Source list is hardcoded in `run_spike.py :: SOURCE_DOCS` and mirrors the docs in the NotebookLM "Substack - Markets" notebook plus `firm_context.yaml :: google_docs.market_update_inputs` if present.
- **Distribution list:** `~/cos-pipeline-config-tomac/distributions/morning-briefing.yaml` (per-tenant). Do NOT inline recipients here.
- **Master Doc:** ID from `firm_context.yaml :: google_docs.daily_market_update`. Default `1UZ1t4bhgzll5VcAuP3Mj1CyYb-4xjgmbUK1xg6oUS_k`.

## STEPS

### 1. Pre-flight

Verify token scopes (same check as `notebooklm-daily-briefing` Step 0). Verify `ANTHROPIC_API_KEY` in env (or `claude` CLI on PATH if subscription mode). Verify domain prompt template file exists.

```bash
DOMAIN=$(python3 -c "import yaml; print(yaml.safe_load(open('${HOME}/cos-pipeline/firm_context.yaml')).get('domain','infra-pe'))")
PROMPT_TEMPLATE="${HOME}/cos-pipeline/domains/${DOMAIN}/prompts/briefing-morning.txt"
test -f "$PROMPT_TEMPLATE" || { echo "ABORT: missing $PROMPT_TEMPLATE"; exit 2; }
echo "Domain: $DOMAIN"
echo "Template: $PROMPT_TEMPLATE"
```

### 2. Run the harness

```bash
DATE=$(python3 -c "from datetime import date; print(date.today())")
OUTFILE="/tmp/briefing_spike_${DATE}.txt"
python3 ~/cos-pipeline/skills/briefing-morning/run_spike.py --out "$OUTFILE"
```

The harness:
1. Loads source docs from Drive (read-only).
2. Builds the prompt by substituting placeholders into the domain template (`{{date}}`, `{{firm_name}}`, `{{principal_name}}`, `{{counterparties_csv}}`, `{{sector_focus_csv}}`, `{{open_deals_csv}}`, `{{recent_actions_csv}}`, `{{source_excerpts}}`).
3. Calls Claude Opus 4.7 (`claude-opus-4-7`, max_tokens=4096, per CLAUDE.md Pass 2 — synthesis-grade reasoning).
4. Writes the memo to `$OUTFILE`.

The default mode is `--dry-run` (prints prompt only, no API call). For a real run, pass `--no-dry-run` explicitly.

### 3. Skip-on-empty gate (mirrors notebooklm SKILL Step 7c)

Before writing or emailing, scan output for "no new content has been published", "no new market developments to report", "there are no new developments", "there are no new market" (case-insensitive).

If any phrase matches → log `SKIP — no new content. Doc and email suppressed.` and exit 0. Do NOT touch the master Doc. Do NOT send email.

### 4. Six-section validation

Before writing, confirm the output contains all six headings exactly (per CLAUDE.md):

- THE CORE ARGUMENT
- POINTS OF CONSENSUS
- POINTS OF DISAGREEMENT OR TENSION
- OPEN QUESTIONS AND UNRESOLVED ISSUES
- WHAT YOU WOULD NEED TO FORM A VIEW
- KEY NAMES AND FIRMS
- ACTION ITEMS

If any heading is missing → log `MALFORMED — missing section <X>. Suppressing write/email; saving raw to $OUTFILE for review.` and exit 0 with a non-success status note.

### 5. Write to master Doc

```bash
python3 ~/credentials/notebooklm_doc_writer.py "$OUTFILE"
```

(Reuses the existing writer — same Doc ID resolved from `firm_context.yaml :: google_docs.daily_market_update`.)

### 6. Email distribution (Tuesday–Friday only)

```bash
DAY=$(python3 -c "from datetime import date; print(date.today().weekday())")  # 0=Mon, 5=Sat, 6=Sun
case "$DAY" in
  0) echo "Monday — email skipped (covered by Sunday weekly distribution)" ;;
  5|6) echo "Weekend — email skipped" ;;
  *) python3 ~/credentials/send_briefing_email.py "$OUTFILE" ;;
esac
```

Recipients are loaded inside `send_briefing_email.py` from `~/cos-pipeline-config-tomac/distributions/morning-briefing.yaml`. Do not inline emails in this SKILL (P1 privacy rule).

### 7. Report

State: domain used, prompt template path, source doc count, model (`claude-opus-4-7`), input/output token estimate, elapsed time, six-section validation result, gate outcome (SKIP / WROTE / WROTE+EMAILED / MALFORMED), and whether email was sent or skipped (with reason).

## RULES

- This SKILL is a thin wrapper. The Python harness (`run_spike.py`) is the source of truth for prompt construction, source loading, and API calls.
- Do not modify `notebooklm-daily-briefing` — it is the baseline against which this SKILL is being compared (Track G).
- Do not inline tenant data (recipient emails, Doc IDs, counterparty lists). Everything tenant-specific resolves at runtime from `firm_context.yaml` or the per-tenant config dir.
- Default to `--dry-run`. Only the production LaunchAgent (post-cut-over) should pass `--no-dry-run`.
- If the Drive sync from `run-syncall-gas` (Substack sync) hasn't completed today, the source docs may be stale. Check `/tmp/new_substack_articles_${DATE}.json` mtime before running, same as the legacy SKILL does in Step 1b.
- On any internal error: log `~/cos-pipeline/logs-tomac/briefing_morning.log`, exit non-zero, do NOT touch master Doc.
