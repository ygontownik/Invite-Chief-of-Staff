# substack-sync (template)

Apps Script that pulls Substack newsletters (and optionally other research-vendor emails) from Gmail, groups by author, and writes one Google Doc per author into a shared Drive folder. Triggered daily at 6am and 7am EST.

## Files

- `Code.template.gs` — sanitized template. RBN scraping + leaked password are OMITTED entirely. All Doc IDs and senders are placeholders.
- `SUBSTACK_SYNC_SECURITY_NOTE.md` — security verification artifact (gitignore + secret scrub log).
- `Code.gs` — local working snapshot of the live editor source. **GITIGNORED. NEVER COMMIT.**

## Placeholders to substitute

| Placeholder | Source |
|---|---|
| `__SUBSTACK_FEEDS_FOLDER_ID__` | `firm_context.yaml :: google_docs.substack_feeds_folder` (or `__OTTER_FOLDER_IDS__`-style per-tenant config) |
| `__SUBSTACK_SENDERS_QUERY__` | `~/cos-pipeline-config-<tenant>/research-vendors/substack.yaml :: senders[]` joined as `(from:a OR from:b)` |
| `__SUBSTACK_DAYS_BACK__` | integer; default `14` |
| `__RBN_PASSWORD_FROM_KEYCHAIN__` | sentinel only; never replace with a literal in committed code |

`setup.sh --instance=<short>` performs substitution at install time.

## Deploy to Apps Script

1. Sign into the tenant's Google account → script.google.com → New project.
2. Paste rendered `Code.gs` (rendered locally; do not commit the rendered output).
3. Run `setupTwoTriggers()` from the editor.
4. Approve Gmail + Drive + Documents scopes.
5. Verify by running `syncSubstackToGoogleDocs()` once manually.

## Secret rotation

- **RBN password (legacy):** the live primary-tenant editor previously contained a hardcoded RBN password inside `backfillRBN()`. Rotation steps:
  1. Reset the RBN account password in the RBN portal.
  2. Open the live Apps Script editor → delete `backfillRBN()`, `rbnLogin()`, and any helpers that referenced the literal.
  3. Confirm the project's collaborator list — anyone with prior read access saw the old password; remove unauthorized collaborators.
  4. If RBN scraping is needed going forward, store the new password ONLY in Script Properties (key `RBN_PASSWORD`) and read via `PropertiesService.getScriptProperties().getProperty('RBN_PASSWORD')`. Never inline.
- **Other secrets:** none in this template. Gmail access is delegated through the script owner's OAuth.

## Why some functions are missing

The live primary-tenant Apps Script project also contains `syncCapstone()`, `syncTranscriptSummaries()`, RBN helpers, one-off fixers, and a `doPost()` web app endpoint. Those are NOT included in the template because:
- The local snapshot is partial (was never fully clasp-cloned, by design — see security note).
- Several helpers carry the leaked-credential blast radius.
- Per-tenant decisions about which sync functions to enable should be explicit at install.

## References

- `~/cos-pipeline/APPS_SCRIPTS_INVENTORY.md` § 3
- `~/cos-pipeline/google-scripts/substack-sync/SUBSTACK_SYNC_SECURITY_NOTE.md`
- `~/cos-pipeline/google-scripts/MULTI_TENANT_PORTING.md`
