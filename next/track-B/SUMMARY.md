# Track B (PLAN J) — multi-tenant scaffolding — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files created (NEW only — nothing existing modified)

- `~/cos-pipeline/multi_tenant.py` — pure-Python tenant scaffolding module
- `~/cos-pipeline/tests/test_multi_tenant.py` — 21 unittest cases, all passing
- `~/cos-pipeline/J_SETUP.md` — onboarding guide, slug→resource table, gotchas, J1–J7 trace
- `~/cos-pipeline-config-re-dev/firm_context.yaml.template` — sanitized template (sibling of populated stub)

No existing `.py`, HTML, plist, or credential file was touched. No LaunchAgent loaded, no port bound, no OAuth flow run.

## Verification

```bash
python3 ~/cos-pipeline/multi_tenant.py                # prints resource table for tomac + re-dev
python3 ~/cos-pipeline/tests/test_multi_tenant.py    # Ran 21 tests in 0.001s — OK
```

Self-test confirmed: tomac→7777, re-dev→7778, labels `com.cos.<slug>.<routine>`, keychain `cos-pipeline-<slug>`, paths under `~/cos-pipeline/data-<slug>` and `logs-<slug>`, registry path `~/cos-pipeline/data-shared/tenant-ports.json`, `list_known_tenants()` returns `['re-dev', 'tomac']`.

## Decisions exercised (existing C-numbers; no new contracts added)

| Ref | Use |
|-----|-----|
| C5  | `re-dev` recognized; reserved-port table |
| C6  | Port table tomac=7777, re-dev=7778; dynamic alloc starts 7779 |
| C7  | `launchagent_label()` → `com.cos.<slug>.<routine>` |
| C11 | `keychain_service()` → `cos-pipeline-<slug>` |
| C12 | Template lists `real-estate | infra-pe | generic-dealmaker` |

## Run-2 judgment calls (paper-only)

- **Dynamic port range = 7779–7977** (199 slots). Inside user-space, clear of common dev ports. Codify as C17 if desired.
- **Reserved slug set = {shared, all, default, common, template}**. Driven by `data-shared/`. Codify as C18 if desired.
- `list_known_tenants()` excludes legacy un-suffixed `cos-pipeline-config/` (predates slug convention).

## Contradictions

None. Existing `~/cos-pipeline-config-re-dev/firm_context.yaml` (run 2 Phase 1.8) already reserves port 7778 and uses keychain prefix `cos-pipeline-re-dev` — confirmed consistent.

## Deferrals (per PLAN J)

- **J4** — Tailscale install (host op, not code)
- **J5** — username/password auth verification (needs running dashboard)
- **J6** — Anthropic/Drive quota & account collision check (paper note in J_SETUP §3 — cannot enforce without live API)
- **J7** — `setup.sh --instance=<short>` shell wrapper (the Python module is its core)

## What's intentionally NOT done

- `data-shared/tenant-ports.json` not created (lifecycle owned by onboarding)
- `data-re-dev/`, `logs-re-dev/` not created
- No plist authored, no Keychain entries added, no LaunchAgent loaded
- No git remote configured for `cos-pipeline-config-re-dev`
