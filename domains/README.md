# Domain bundles — Track M-MIN

This directory holds **domain abstraction bundles** for the COS pipeline. A domain bundle defines the pipeline stages, counterparty vocabulary, deal keywords, email-triage rules, prompt templates, and model routing for a class of users (infra-PE firm, real-estate developer, generic dealmaker).

Loaded when `firm_context.yaml :: domain` is set on a tenant. See PLAN_v3.1.md Track M-MIN and DECISIONS.md C12/C13.

## Currently shipped domains

| Domain | Slug | Use case |
|---|---|---|
| Infrastructure private equity | `infra-pe` | Tomac Cove and similar small/mid PE infra GPs. Six-section investor memo per CLAUDE.md. Heavy sector keyword set. |
| Real estate developer | `real-estate` | Solo or small-team RE operators. Building/site/financing pipeline. No public-comp tracking, no research vendor feeds by default. |
| Generic dealmaker | `generic-dealmaker` | Domain-neutral fallback. Sales / BD / advisor / solo operator. Lean stage list, lean keywords. |

## Bundle layout

```
domains/<name>/
  config.yaml          — schema below
  prompts/
    briefing-morning.txt
    deal-summary.txt
    email-triage.txt
```

## config.yaml schema (Track M-MIN.2)

```yaml
domain: <name>
pipeline_stages: [list of strings, ordered]
counterparty_types: [list of strings]
deal_keywords: [list of strings — used by capture/triage scoring]
email_triage_rules:
  high_priority_senders_role: [list]
  auto_archive_keywords: [list]
  flag_for_followup_keywords: [list]
  research_routing_keywords: [list]
default_research_vendors: [list of vendor slugs]
promote_owners_default: [list — usually empty; tenant fills via firm_context.yaml]
memo_section_names: [list of section headings, in order, used by Pass 3 IC memo prompt]
model_routing:
  pass1_source_scanner:
    model: <claude model id>
    max_tokens: <int>
  pass2_pipeline_analyst:
    model: <claude model id>
    max_tokens: <int>
  pass3_ic_memo:
    model: <claude model id>
    max_tokens: <int>
```

## Prompt template format

Each `prompts/*.txt` is plain text with `{{double-curly}}` placeholders. The model router (Track C) and onboarding (Track D) substitute placeholders with values from `firm_context.yaml` and runtime context before calling Claude.

Common placeholders:
- `{{firm_name}}`, `{{principal_name}}`, `{{date}}`
- `{{counterparties_csv}}`, `{{open_deals_csv}}`, `{{deal_keywords_csv}}`
- `{{source_excerpts}}`, `{{email_batch}}`
- Domain-specific: `{{sector_focus_csv}}` (infra-pe), `{{markets_csv}}` (real-estate), `{{address}}` (real-estate)

Every prompt file leads with a `## STATUS: DRAFT — needs validation against domain expert` header. Validation gates promotion to STATUS: PRODUCTION.

## How the bundle is loaded

### Track C — model router (`_model_router.py`, planned)

```python
from pathlib import Path
import yaml

def load_domain_bundle(domain: str) -> dict:
    base = Path.home() / "cos-pipeline" / "domains" / domain
    config = yaml.safe_load((base / "config.yaml").read_text())
    prompts = {p.stem: p.read_text() for p in (base / "prompts").glob("*.txt")}
    return {"config": config, "prompts": prompts}
```

The router calls `load_domain_bundle(firm_context["domain"])` once at process start, caches it, and uses:
- `config["model_routing"][pass_name]` to pick model + max_tokens for each pass
- `config["deal_keywords"]` and `config["email_triage_rules"]` as defaults merged with `firm_context.yaml :: deal_keywords` (tenant overrides win)
- `prompts[<template_name>]` rendered with tenant + runtime vars

### Track D — onboarding (`setup.sh --domain=<name>`)

The onboarding wizard reads the `--domain` flag, validates it against the directory listing here, and writes `domain: <name>` into the new tenant's `firm_context.yaml`. It also seeds the tenant's pipeline doc with the `pipeline_stages` and `counterparty_types` from the bundle.

## Adding a fourth domain

1. Pick a slug (lowercase, hyphenated, no firm names): e.g. `venture-capital`, `family-office`, `m-and-a-advisor`.
2. `mkdir -p ~/cos-pipeline/domains/<slug>/prompts`
3. Author `config.yaml` matching the schema above. Start by copying `generic-dealmaker/config.yaml` and tightening.
4. Author the three prompt templates. Lead each with `## STATUS: DRAFT`.
5. Update DECISIONS.md C12 to add the new domain to the allowed values.
6. Update this README's "Currently shipped domains" table.
7. Validate against a real user from the target domain before promoting any prompt to STATUS: PRODUCTION.

## Hard rules

- **No firm names, no PII, no Doc IDs in this directory.** Domain bundles are public-repo-safe. Tenant-specific values live in `firm_context.yaml` and `~/cos-pipeline-config-<slug>/`.
- **No code in this directory.** Bundles are pure data + text. The router (Track C) is the only code that reads them.
- **No model IDs hardcoded outside `config.yaml :: model_routing`.** The router must respect what the bundle says.
