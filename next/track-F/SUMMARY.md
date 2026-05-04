# Track F — Apps Scripts templating

Run 2, 2026-05-03. Sub-agent track.

## Files created

| Path | Purpose |
|---|---|
| `google-scripts/otter-drive-watcher/Code.template.gs` | Template; `FOLDER_IDS` → `__OTTER_FOLDER_IDS__` |
| `google-scripts/otter-drive-watcher/README.md` | Populate / deploy / rotation guide |
| `google-scripts/beside-action-router/Code.template.gs` | Template; 3 Doc IDs + 2 default strings → placeholders |
| `google-scripts/beside-action-router/README.md` | Populate / deploy guide + Wave 2 architecture flag |
| `google-scripts/substack-sync/Code.template.gs` | Template; folder ID, sender query, days-back → placeholders. RBN backfill OMITTED. |
| `google-scripts/substack-sync/README.md` | Populate / deploy guide + RBN rotation steps |
| `google-scripts/substack-sync/SUBSTACK_SYNC_SECURITY_NOTE.md` | Detailed gitignore + secret-scrub verification |
| `google-scripts/SUBSTACK_SYNC_SECURITY_NOTE.md` | Top-level pointer |
| `.gitignore` (modified) | Replaced blanket `substack-sync/` block with file-glob rules so the template + README can be committed while live `Code.gs` stays blocked |

## Decisions

1. Template path: `google-scripts/<name>/Code.template.gs` per brief.
2. Substack template is committable. Updated `.gitignore`: kept `substack-sync*/Code.gs|Code.js|appsscript.json|.clasp.json` and `*-clasped/` blocked; removed the blanket directory block (which would have made re-include impossible per gitignore semantics) in favor of file-glob rules.
3. RBN backfill omitted entirely from substack template (not just placeholder-swapped) to minimize blast radius. Sentinel `__RBN_PASSWORD_FROM_KEYCHAIN__` appears only in the security comment block as documentation.
4. Beside-action-router gained `__DEFAULT_WORKSTREAM__` and `__DEFAULT_OWNER__` placeholders to remove "Tomac Cove" / "Yoni" tenant tokens.

## Deferrals

1. No clasp/OAuth/push performed (per brief).
2. The non-substack `Code.gs` snapshots are not yet gitignored — recommend a follow-up adding `google-scripts/*/Code.gs` with template exception once Track J finalizes the install flow. Out of Track F scope.
3. Did not capture missing substack functions (`syncCapstone`, `syncTranscriptSummaries`, `doPost`, etc.). Per-tenant decision at install.
4. RBN password rotation is a manual operator step in the live editor; documented in substack-sync README.

## Verification commands

```bash
cd ~/cos-pipeline && git check-ignore -v google-scripts/substack-sync/Code.gs
cd ~/cos-pipeline && git check-ignore -v google-scripts/substack-sync/Code.template.gs ; echo "exit=$?"
cd ~/cos-pipeline && git status --porcelain google-scripts/ | grep -i substack ; echo "exit=$?"
cd ~/cos-pipeline && git ls-files --others --ignored --exclude-standard google-scripts/
cd ~/cos-pipeline && git ls-files --others --exclude-standard google-scripts/ | grep template
grep -RIn -e 'PASS=' -e 'PASSWORD=' ~/cos-pipeline/google-scripts/*/Code.template.gs ~/cos-pipeline/google-scripts/*/README.md
```

## Observed verification (2026-05-03)

- `Code.gs` matched by rule `google-scripts/substack-sync*/Code.gs` (ignored).
- `Code.template.gs` + `README.md` exit 1 from `check-ignore` (allowed).
- `git ls-files --ignored` returns exactly: `google-scripts/substack-sync/Code.gs`.
- `git status --porcelain google-scripts/ | grep -i substack` → exit 1.
- No literal RBN password anywhere in committed-eligible files.

## Hand-off

- Track CLASP: still blocked on RBN rotation per `APPS_SCRIPTS_INVENTORY.md` § 3.
- Track J / setup.sh: substitution maps documented in each script's README.
- Wave 2: beside-action-router → Python SKILL port is open question.
