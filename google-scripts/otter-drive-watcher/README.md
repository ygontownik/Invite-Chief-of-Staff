# otter-drive-watcher (template)

Apps Script that polls Otter-managed Drive folders and POSTs to the dashboard's `/otter-webhook` endpoint when new transcripts arrive.

## Files

- `Code.template.gs` — sanitized template. All folder IDs replaced with placeholder `__OTTER_FOLDER_IDS__`. Webhook secret and server URL are read from Script Properties (already secret-clean in source).
- `Code.gs` — local-only working snapshot for the primary tenant. Gitignored at the repo level via the broader `.tools/` and per-tenant patterns; do NOT commit.

## Populate per tenant

1. Read `firm_context.yaml :: otter_folders[]` for the target tenant.
2. Render `Code.template.gs` by substituting `__OTTER_FOLDER_IDS__` with a JS array literal, e.g.:
   ```js
   var FOLDER_IDS = ["1abc...", "1def..."];
   ```
3. Save the rendered output as `Code.gs` in the tenant working dir (NOT under `google-scripts/<slug>/Code.gs` if you intend to commit).

## Deploy to Apps Script

Manual flow (no clasp login from CI):

1. In a browser, sign into the tenant's Google account → script.google.com → New project.
2. Paste rendered `Code.gs`.
3. Project Settings → Script Properties → add:
   - `SERVER_URL` = tenant dashboard webhook URL
   - `WEBHOOK_SECRET` = freshly-generated per-tenant secret (also stored in `~/cos-pipeline-config-<tenant>/secrets`)
4. Run `installTrigger()` once from the editor; approve Drive + UrlFetch scopes.
5. Run `testWebhook()` to verify connectivity.

## Secret rotation

- `WEBHOOK_SECRET`: generate new value, update both Script Properties and dashboard server config, restart dashboard server.
- No other secrets in this script.

## References

- `~/cos-pipeline/APPS_SCRIPTS_INVENTORY.md` § 1
- `~/cos-pipeline/google-scripts/MULTI_TENANT_PORTING.md`
