# beside-action-router (template)

Apps Script that scans the "Beside Notes & Memos" Google Doc every 15 minutes, parses ACTION ITEMS blocks, and routes each action to the Follow-ups Doc and/or Deal Pipeline Doc per the action's `Dashboard:` field.

## Files

- `Code.template.gs` — sanitized template. All Doc IDs and per-tenant defaults replaced with placeholders.
- `Code.gs` — local working snapshot for the primary tenant. Do NOT commit if it contains live Doc IDs.

## Placeholders to substitute

| Placeholder | Source |
|---|---|
| `__BESIDE_DOC_ID__` | `firm_context.yaml :: google_docs.beside_notes` |
| `__FOLLOWUPS_DOC_ID__` | `firm_context.yaml :: google_docs.followups` |
| `__DEAL_DOC_ID__` | `firm_context.yaml :: google_docs.deal_pipeline` |
| `__DEFAULT_WORKSTREAM__` | `firm_context.yaml :: principal.default_workstream` |
| `__DEFAULT_OWNER__` | `firm_context.yaml :: principal.name` |

`setup.sh --instance=<short>` performs this substitution at install time.

## Deploy to Apps Script

1. Sign into the tenant's Google account → script.google.com → New project.
2. Paste rendered `Code.gs`.
3. Run `installTrigger()` from the editor; approve permissions.
4. Verify by writing a test ACTION ITEMS block into the Beside doc and waiting up to 15 minutes (or running `routeNewActions()` manually).

## Secret rotation

No secrets. Doc IDs are tenant-confidential but not credentials.

## Architecture flag

Per `APPS_SCRIPTS_INVENTORY.md` § 2: this is the only ACTION ITEMS router in the system. Open question for Wave 2 — should it be ported to a Python `actions-router` SKILL so all action sources route through one place per tenant. Until then, this remains the sole router and ships per-tenant.

## References

- `~/cos-pipeline/APPS_SCRIPTS_INVENTORY.md` § 2
- `~/cos-pipeline/google-scripts/MULTI_TENANT_PORTING.md`
