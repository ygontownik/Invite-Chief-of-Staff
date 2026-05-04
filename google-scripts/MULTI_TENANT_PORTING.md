# MULTI_TENANT_PORTING.md — Apps Script clasp deployment for second-tenant installs

Per PLAN_v3.1.md Track A-SLOW.3 + Privacy P4 + the multi-tenant story:
the three Apps Script projects in `~/cos-pipeline/google-scripts/` contain
**hardcoded Tomac-tenant Google Doc IDs**. They cannot be cloned for a second
tenant without first being templated. This document specifies the porting
contract.

---

## Three Apps Script projects in scope

| Project | Purpose | Tenant-coupled values |
|---------|---------|------------------------|
| `beside-action-router` | Routes Beside-AI capture entries into Followups / Deal pipeline docs | 3 hardcoded Doc IDs |
| `otter-drive-watcher` | Watches Otter folders in Drive for new transcripts | N hardcoded Folder IDs |
| `substack-sync` | Pulls Substack RSS into a per-feed Doc archive | 1 hardcoded Folder ID, plus a hardcoded `backfillRBN()` credential |

---

## Placeholder convention

When the second-tenant installer (`setup.sh`) runs, it must produce a clean
template version of each script with these substitutions, **before** running
`clasp push` into the new tenant's account:

| Placeholder token | Replaces (Tomac value) | Source field in `firm_context.yaml` |
|-------------------|------------------------|-------------------------------------|
| `__BESIDE_DOC_ID__` | `1fl05-kYeeJuORNNwiMx0yhnRQWhKZ9Gd-1FtosznHg4` | `drive.beside_capture_doc_id` |
| `__FOLLOWUPS_DOC_ID__` | `10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY` | `drive.followups_doc_id` |
| `__DEAL_DOC_ID__` | `1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc` | `drive.deal_pipeline_doc_id` |
| `__OTTER_FOLDER_IDS__` | JS array of folder IDs in `otter-drive-watcher/Code.gs:28` | `drive.otter_folder_ids` (yaml list → JS array literal) |
| `__SUBSTACK_FEEDS_FOLDER_ID__` | `15cTUBvS63edtT5pM-k8LpacUOSQujSVF` | `drive.substack_feeds_folder_id` |
| `__SUBSTACK_FEEDS__` | JS array of `{name, url}` objects (substack-sync/Code.gs feed table) | `feeds.substack` (yaml list → JS array literal) |

### Substitution sites

- `beside-action-router/Code.gs` lines 22–24: replace the three `var ..._DOC_ID = "..."` literals with the placeholders.
- `otter-drive-watcher/Code.gs` line 28: replace the `var FOLDER_IDS = [ ... ]` array literal with `__OTTER_FOLDER_IDS__`.
- `substack-sync/Code.gs` line 22: replace `var FOLDER_ID = '15c...'` with `__SUBSTACK_FEEDS_FOLDER_ID__`. Also strip the entire `backfillRBN()` function — it contains a credential and is a tenant-specific historical backfill, not a multi-tenant feature. Leave a stub `function backfillRBN() { throw new Error('Not configured for this tenant'); }`.

---

## setup.sh (second-tenant install) sequence

For each Apps Script project:

```bash
# 1. Read tenant config
TENANT_DIR="$HOME/cos-pipeline-config-<tenant>"
FIRM_CTX="$TENANT_DIR/firm_context.yaml"

# 2. Stage a clean working copy of the template
cp -r ~/cos-pipeline/google-scripts/<project>/ "$TENANT_DIR/google-scripts/<project>/"

# 3. Template substitution (yq + sed; values from firm_context.yaml)
BESIDE_DOC_ID=$(yq '.drive.beside_capture_doc_id' "$FIRM_CTX")
FOLLOWUPS_DOC_ID=$(yq '.drive.followups_doc_id' "$FIRM_CTX")
DEAL_DOC_ID=$(yq '.drive.deal_pipeline_doc_id' "$FIRM_CTX")

sed -i '' "s|__BESIDE_DOC_ID__|$BESIDE_DOC_ID|g" "$TENANT_DIR/google-scripts/beside-action-router/Code.gs"
sed -i '' "s|__FOLLOWUPS_DOC_ID__|$FOLLOWUPS_DOC_ID|g" "$TENANT_DIR/google-scripts/beside-action-router/Code.gs"
sed -i '' "s|__DEAL_DOC_ID__|$DEAL_DOC_ID|g" "$TENANT_DIR/google-scripts/beside-action-router/Code.gs"

# 4. Create a NEW Apps Script project under the tenant's account
#    (This requires the tenant to have run `clasp login` with their own Google account.)
cd "$TENANT_DIR/google-scripts/beside-action-router"
~/cos-pipeline/.tools/node_modules/.bin/clasp create --type standalone --title "Beside Action Router (<tenant>)"

# 5. Push the substituted source up
~/cos-pipeline/.tools/node_modules/.bin/clasp push -f

# 6. Capture the new script ID (from .clasp.json) into firm_context.yaml
NEW_ID=$(jq -r '.scriptId' .clasp.json)
yq -i ".apps_script.beside_action_router = \"$NEW_ID\"" "$FIRM_CTX"

# 7. Prompt user for the one-time installTrigger() execution
echo "ACTION REQUIRED: open https://script.google.com/d/$NEW_ID/edit"
echo "  Run the installTrigger() function once to register the time-driven trigger."
echo "  Authorize when prompted."
read -p "Press <enter> when done..." _
```

Repeat for `otter-drive-watcher` and `substack-sync`.

### Why `installTrigger()` must be manual

Apps Script triggers cannot be programmatically authorized via clasp — the
first run requires a human to grant OAuth scopes (Drive, Documents) in the
Google consent UI. There is no API path. `setup.sh` must therefore pause and
prompt the operator after `clasp push`.

---

## Privacy P4 follow-on (separate from this track)

- The current `~/cos-pipeline/google-scripts/<project>/Code.gs` files are the **Tomac-coupled live versions**. They MUST NOT be committed to a public/shared repo.
- A future task should create `~/cos-pipeline/google-scripts/<project>/Code.gs.template` versions with placeholders applied (per the table above), commit those templates, and `.gitignore` the live `Code.gs` files.
- `substack-sync/Code.gs` is permanently `.gitignore`d regardless (contains an unrotated RBN credential in `backfillRBN()`); only the templated/stub version may ever be committed.

---

## Open questions

- Where should `firm_context.yaml :: drive.*` keys live in the canonical schema? (Currently scattered.) Suggest grouping under `drive:` block with stable key names matching the placeholder tokens above (lowercase + snake_case).
- Should `__SUBSTACK_FEEDS__` be a list of `{name, url}` objects in YAML, or maintained in a separate `feeds.yaml`? Recommend separate file given anticipated growth.
- Otter folder IDs: are these per-Otter-account (one user = one set) or shared across a workspace? Affects whether they belong in `firm_context.yaml` (per-tenant) or `principal.yaml` (per-user).
