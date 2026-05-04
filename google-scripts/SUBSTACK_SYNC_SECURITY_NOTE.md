# Substack-sync security verification (top-level pointer)

This file is the top-level deliverable required by Track F. The full content lives at:

- `~/cos-pipeline/google-scripts/substack-sync/SUBSTACK_SYNC_SECURITY_NOTE.md`

Summary:
- Unrotated RBN password is NOT in the repository in any form.
- `__RBN_PASSWORD_FROM_KEYCHAIN__` placeholder is the only credential token in the template.
- `.gitignore` blocks `google-scripts/substack-sync*/Code.gs` and clasp byproducts; allows the sanitized template + docs.
- `git status --porcelain google-scripts/ | grep -i substack` returned exit 1 both before and after producing the template (no match — Substack live source is gitignored).
- Substack live source must NEVER be committed.
