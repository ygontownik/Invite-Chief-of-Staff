# Skill telemetry — dash-state-hook.py integration

Block E created [`skill_telemetry.py`](skill_telemetry.py) as a standalone module
because `dash-state-hook.py` is owned by Chat B in the parallel rollout. This
note is the 3-line patch Chat B should apply once Block E lands.

## Why a Stop-hook job (and not a Claude Code hook in settings.json)

The hook fires on every session end, has access to the transcript file path,
and is already orchestrating periodic jobs (deal-entry sync, reference docs
mirror, intel capture). Skill telemetry is the same pattern: walk the most-
recent transcript(s), append rows to a JSONL, move on.

Doing it in `settings.json` hooks would require a per-tool-call hook, which
fires hot path and isn't worth the latency for a weekly review pass.

## The patch

Add a new periodic job alongside `run_intel_capture_scan()`:

```python
# Skill-telemetry scan (every fire — same cadence as intel capture)
SKILL_TELEMETRY_LOCK = Path("/tmp/dash-state-hook-skills.last")
SKILL_TELEMETRY_INTERVAL = 0  # every fire; idempotent on session_id


def run_skill_telemetry_scan():
    """Append a row for each Skill invocation in the most-recent transcripts."""
    try:
        # Import lazily so a missing module doesn't crash the whole hook.
        sys.path.insert(0, str(COS_PIPELINE_DIR / "tools"))
        import skill_telemetry  # noqa: WPS433
        summary = skill_telemetry.scan_recent_transcripts(limit=3)
        if summary.get("rows"):
            log(f"[skill_telemetry] scanned={summary['scanned']} new_rows={summary['rows']}")
    except Exception as exc:  # noqa: BLE001 — never crash the hook
        log(f"[skill_telemetry] error: {exc}")
```

Then wire it into the `main()` orchestrator alongside `run_intel_capture_scan()`:

```python
    run_intel_capture_scan()
    run_skill_telemetry_scan()   # NEW
```

That's it. The module is idempotent on `session_id` so re-runs are safe.

## Where the data lands

`~/dashboards/data/compiled/skill-telemetry.jsonl` — one row per Skill
invocation. Schema documented at the top of `skill_telemetry.py`.

## Follow-up (not in this block)

A morning-briefing prompt to ask the user to mark `outcome` for each
yesterday-row where it's still `null`. Three buttons: ✓ used / ✏️ edited
/ ✗ discarded. Hits `skill_telemetry.set_outcome(row_id, outcome)`.

Weekly review surfaces skills with >30% edit/discard rate as needing
improvement (audit §3 #8).
