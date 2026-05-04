# launchagents-templates/

LaunchAgent plist templates referenced by the subscription installer.
The installer (`setup.sh.subscription.next`, when integrated) reads
these templates, substitutes placeholders, and writes the result to
`~/Library/LaunchAgents/`.

## Templates

| File | Purpose | Cadence |
|---|---|---|
| `com.cos.SLUG.queue-drain.plist.template` | Drain `data-<slug>/queue.jsonl` of subscription rate-limit failures whose 5-hour window has elapsed. | every 30 min |
| `com.cos.SLUG.subscription-health.plist.template` | Snapshot `_subscription_health.py --json` to `data-<slug>/subscription-health.json` for the dashboard tile. | every 1 hour |

Both templates are no-ops on api-mode tenants — they should only be
installed when `firm_context.yaml :: auth_mode == 'subscription'`.

## Placeholder substitution (install-time)

Two placeholders are replaced before writing to `~/Library/LaunchAgents/`:

- `<SLUG>` — tenant slug (lowercase: `tomac`, `re-dev`, …).
- `<PYTHON>` — absolute path to a Python ≥ 3.10 interpreter
  (subscription mode requires `claude_agent_sdk`, which doesn't import
  on system Python 3.9). The installer probes
  `/opt/homebrew/bin/python3` → `/usr/local/bin/python3` →
  `command -v python3` and picks the first ≥ 3.10 candidate.

Recommended `setup.sh` snippet (sketched, not wired):

```bash
sed -e "s|<SLUG>|$INSTANCE|g" \
    -e "s|<PYTHON>|$PY_BIN|g" \
    "$REPO/launchagents-templates/com.cos.SLUG.queue-drain.plist.template" \
    > "$HOME/Library/LaunchAgents/com.cos.$INSTANCE.queue-drain.plist"
launchctl load "$HOME/Library/LaunchAgents/com.cos.$INSTANCE.queue-drain.plist"
```

## Why these are templates, not generated on the fly

The `_scheduler.py` registration helper (Build #6 / `setup.sh
--uninstall`) walks `~/Library/LaunchAgents/com.cos.<slug>.*.plist`
to find what to unload + remove. Stamping per-tenant plists from
hand-curated templates keeps the schema reviewable and stable;
each template can grow (e.g. `LowPriorityIO`, `EnableTransactions`)
without touching the install pipeline.

## Uninstall

`setup.sh --instance=<slug> --uninstall` (Build #6) walks every
`com.cos.<slug>.*` LaunchAgent label, unloads them, and removes the
plist files. No template-side cleanup needed.
