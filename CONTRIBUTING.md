# Contributing to the COS Pipeline

This repo is the shared codebase behind a portable Chief of Staff AI system. It is designed to work for any firm — any principal name, any investment focus, any team structure — with all firm-specific identity living in gitignored config files that never touch GitHub.

There are two kinds of changes: **universal** (belongs here) and **personal** (belongs in your own config). The distinction determines whether something should be a PR or just stay in your fork.

---

## Universal changes — open a PR

These make the system better for any firm, regardless of who they are:

- Bug fixes in any `.py` file
- New pipeline features or integrations (new email provider, new data source, OneDrive support)
- Improvements to `DEFAULT_MEMO_SECTIONS` or `DEFAULT_SECTION_GUIDANCE` in `_firm_context.py`
- New optional fields added to `firm_context.template.yaml` or `firm_config.template.json`
- Dashboard, setup, or documentation improvements
- Better error handling, cost tracking, or logging
- Performance improvements (prompt caching, batching, deduplication)

**The test:** would this change make the system better for someone you've never met, at a firm you know nothing about?

---

## Personal changes — keep in your fork or your YAML

These belong to your firm, not to the shared codebase:

- Your firm's memo sections, analytical framing, or investment thesis (`prompt_overrides` in your `firm_context.yaml`)
- Changes that only make sense for your sector, deal type, or team structure
- Anything referencing a specific firm name, deal name, person, or Google Doc ID
- Your `draft_voice` rules, `deal_keywords`, `peer_firms`, or `owner_whitelist`

**The test:** does this change require knowing who you are? If yes, it's personal — keep it in `firm_context.yaml`.

---

## How to open a PR

1. Fork `github.com/ygontownik/Invite-Chief-of-Staff`
2. Create a branch in your fork: `git checkout -b fix/speaker-attribution`
3. Make your change — keep it focused, one thing per PR
4. Verify nothing firm-specific leaked in: `git diff --cached` — no real names, no Doc IDs, no credentials
5. Open a PR against `ygontownik/Invite-Chief-of-Staff:main` with a short description of what changed and why it's universal

PRs are reviewed by the repo maintainer. A change that's useful but too firm-specific will be declined with a note — keep it in your fork.

---

## What never belongs in a PR

- `firm_context.yaml` — gitignored, should never appear in a diff
- `firm_config.json` — same
- `~/credentials/` — OAuth tokens, client secrets, API keys
- Real Google Doc IDs, Drive folder IDs, or any live credential
- Personal briefing content, deal names, LP names, or recruiting activity

If you accidentally staged one of these, `git reset HEAD <file>` before committing.

---

## How the update model works for users

When a universal improvement is merged to `main`, you get it by running:

```bash
git fetch upstream
git merge upstream/main
```

Your `firm_context.yaml` is gitignored — it is never touched by a merge. If you have overridden a default (e.g. `prompt_overrides.memo_sections`), your version takes precedence and the upstream change to `DEFAULT_MEMO_SECTIONS` does not affect you. If you have not overridden it, you get the improvement automatically.

To accept an upstream default after you've overridden it: delete that key from your `firm_context.yaml` and the default kicks back in on the next run.
