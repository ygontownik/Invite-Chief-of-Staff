#!/usr/bin/env python3
"""
generate-system-map.py — produces ~/dashboards/docs/SYSTEM-MAP.md

Walks the existing config + code surfaces and assembles a single
canonical map of:
  - every Drive doc + folder (writers/readers) — from drive-docs.yaml
  - every scheduled task — from schedule.yaml + scheduled-tasks/*/SKILL.md
  - every slash command — from ~/.claude/commands/*.md
  - the Stop hook — from dash-state-hook.py
  - per-deal data files — from data/deals/<deal>/
  - dashboard tiles — from dashboard-tiles.yaml

Run manually OR auto-triggered by dash-state-hook when drive-docs.yaml
or schedule.yaml changes.

Output: ~/dashboards/docs/SYSTEM-MAP.md (overwritten)
"""
import os
import re
import yaml
import json
import subprocess
from datetime import datetime
from pathlib import Path

import glob

HOME = Path.home()

# Tenant-agnostic discovery (mirrors dash-state-hook.py).
DASHBOARDS = Path(os.environ.get("COS_DATA_DIR", str(HOME / "dashboards")))
COS_PIPELINE = HOME / "cos-pipeline"


def _find_config_dir():
    if os.environ.get("COS_CONFIG_DIR"):
        return Path(os.environ["COS_CONFIG_DIR"])
    matches = sorted(glob.glob(str(HOME / "cos-pipeline-config-*")))
    populated = [m for m in matches if (Path(m) / "drive-docs.yaml").exists()]
    if populated:
        return Path(populated[0])
    if matches:
        return Path(matches[0])
    return HOME / "cos-pipeline-config"


COS_CONFIG = _find_config_dir()
CLAUDE_CMDS = HOME / ".claude" / "commands"

OUTPUT = DASHBOARDS / "docs" / "SYSTEM-MAP.md"


def safe_yaml(path):
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def section_drive_docs():
    cfg = safe_yaml(DASHBOARDS / "config" / "drive-docs.yaml")
    out = ["## Drive artifacts (from drive-docs.yaml)\n"]

    docs = cfg.get("docs", {}) or {}
    out.append("### General docs\n")
    out.append("| Key | Name | Writers | Readers |")
    out.append("|---|---|---|---|")
    for k, d in docs.items():
        writers = ", ".join(d.get("writers", [])) if isinstance(d.get("writers"), list) else d.get("writer", "")
        readers = ", ".join(d.get("readers", [])) if isinstance(d.get("readers"), list) else d.get("reader", "")
        out.append(f"| {k} | {d.get('name','')} | {writers or '_none_'} | {readers or '_none_'} |")
    out.append("")

    refs = cfg.get("reference_docs", {}) or {}
    if refs:
        out.append("### Reference docs (loaded by every Claude session)\n")
        out.append("| Key | Name | Mirror path | Writers |")
        out.append("|---|---|---|---|")
        for k, d in refs.items():
            writers = ", ".join(d.get("writers", [])) if isinstance(d.get("writers"), list) else ""
            out.append(f"| {k} | {d.get('name','')} | {d.get('mirror_path','')} | {writers} |")
        out.append("")

    deal_docs = cfg.get("deal_docs", {}) or {}
    if deal_docs:
        out.append("### Deal docs (per-deal status + master_brief)\n")
        out.append("| Deal | Folder | Status doc | Master brief |")
        out.append("|---|---|---|---|")
        for k, d in deal_docs.items():
            s = d.get("status", {}).get("doc_id", "")
            b = d.get("master_brief", {}).get("doc_id", "")
            f = d.get("drive_folder_id", "")
            out.append(f"| {k} | `{f}` | `{s}` | `{b}` |")
        out.append("")

    folders = cfg.get("folders", {}) or {}
    out.append("### Drive folders\n")
    out.append("| Key | Name | Writers | Readers |")
    out.append("|---|---|---|---|")
    for k, d in folders.items():
        writers = ", ".join(d.get("writers", [])) if isinstance(d.get("writers"), list) else ""
        readers = ", ".join(d.get("readers", [])) if isinstance(d.get("readers"), list) else ""
        out.append(f"| {k} | {d.get('name','')} | {writers or '_none_'} | {readers or '_none_'} |")
    out.append("")

    local = cfg.get("local_state", {}) or {}
    out.append("### Local state files (dedup + run state)\n")
    out.append("| Key | Path | Writer |")
    out.append("|---|---|---|")
    for k, d in local.items():
        w = d.get("writer") or ", ".join(d.get("writers", []))
        out.append(f"| {k} | `{d.get('path','')}` | {w} |")
    out.append("")

    return "\n".join(out)


def section_scheduled_tasks():
    sched_path = DASHBOARDS / "config" / "schedule.yaml"
    sched = safe_yaml(sched_path)
    out = ["## Scheduled tasks (from schedule.yaml)\n"]
    out.append("| Task | Cron | Group | Skill |")
    out.append("|---|---|---|---|")
    routines = sched.get("routines", []) if isinstance(sched, dict) else []
    if not routines and isinstance(sched, list):
        routines = sched
    for r in routines:
        if not isinstance(r, dict):
            continue
        out.append(f"| {r.get('name','')} | `{r.get('cron','')}` | {r.get('group','')} | {r.get('skill','')} |")
    out.append("")

    tasks_dir = DASHBOARDS / "scheduled-tasks"
    out.append("### Scheduled-task SKILL.md inventory\n")
    out.append("| Task dir | Has SKILL.md? | One-liner |")
    out.append("|---|---|---|")
    if tasks_dir.exists():
        for d in sorted(tasks_dir.iterdir()):
            if not d.is_dir():
                continue
            skill = d / "SKILL.md"
            has = skill.exists()
            one = ""
            if has:
                try:
                    txt = skill.read_text()
                    m = re.search(r"^description:\s*(.+)$", txt, re.M)
                    if m:
                        one = m.group(1).strip()[:90]
                    else:
                        for line in txt.split("\n"):
                            line = line.strip()
                            if line and not line.startswith("#") and not line.startswith("---"):
                                one = line[:90]
                                break
                except Exception:
                    pass
            out.append(f"| {d.name} | {'yes' if has else 'no'} | {one} |")
    out.append("")
    return "\n".join(out)


def section_slash_commands():
    out = ["## Slash commands (~/.claude/commands)\n"]
    out.append("| Command | Description |")
    out.append("|---|---|")
    if CLAUDE_CMDS.exists():
        for f in sorted(CLAUDE_CMDS.glob("*.md")):
            txt = f.read_text()
            m = re.search(r"^description:\s*(.+)$", txt, re.M)
            desc = m.group(1).strip() if m else ""
            out.append(f"| `/{f.stem}` | {desc} |")
    out.append("")
    return "\n".join(out)


def section_stop_hook():
    hook = DASHBOARDS / "scripts" / "dash-state-hook.py"
    out = ["## Stop hook — dash-state-hook.py\n"]
    if not hook.exists():
        return "\n".join(out + ["_hook file not found_"])
    txt = hook.read_text()

    intervals = re.findall(r"(\w+_INTERVAL)\s*=\s*(\d+)", txt)
    out.append("Periodic jobs the hook fires from `main()`:\n")
    out.append("| Constant | Interval (s) | Interval (human) |")
    out.append("|---|---|---|")
    for k, v in intervals:
        secs = int(v)
        if secs >= 86400:
            human = f"{secs//86400}d"
        elif secs >= 3600:
            human = f"{secs//3600}h"
        else:
            human = f"{secs//60}m"
        out.append(f"| {k} | {secs} | {human} |")
    out.append("")

    funcs = re.findall(r"^def (run_[a-z_]+)\(", txt, re.M)
    out.append("Periodic job entry points:\n")
    for f in funcs:
        out.append(f"- `{f}()`")
    out.append("")
    return "\n".join(out)


def section_per_deal_files():
    out = ["## Per-deal data files (~/dashboards/data/deals/)\n"]
    deals_dir = DASHBOARDS / "data" / "deals"
    if not deals_dir.exists():
        return "\n".join(out + ["_deals dir not found_"])
    out.append("| Deal | log.json | deal.md | actions.md | profit-model.xlsx |")
    out.append("|---|---|---|---|---|")
    for d in sorted(deals_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        files = {f.name: f.exists() and f.stat().st_size for f in [
            d / "log.json", d / "deal.md", d / "actions.md", d / "profit-model.xlsx"
        ]}
        marks = []
        for f in ("log.json", "deal.md", "actions.md", "profit-model.xlsx"):
            sz = files.get(f, 0)
            marks.append(f"{sz}b" if sz else "—")
        out.append(f"| {d.name} | {marks[0]} | {marks[1]} | {marks[2]} | {marks[3]} |")
    out.append("")
    out.append("`log.json` is the **canonical per-deal intel feed** — written by `cos_capture_pipeline.py` (Gmail/Otter/calendar/awaiting-external) and read by `/deal-sync` to regenerate status + brief docs.\n")
    return "\n".join(out)


def section_compiled_outputs():
    out = ["## Compiled artifacts (~/dashboards/data/compiled/)\n"]
    cdir = DASHBOARDS / "data" / "compiled"
    if not cdir.exists():
        return "\n".join(out + ["_compiled dir not found_"])
    out.append("| Artifact | Size | Top-level keys (if JSON) |")
    out.append("|---|---|---|")
    for f in sorted(cdir.iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue
        sz = f.stat().st_size
        keys = ""
        if f.suffix == ".json":
            try:
                d = json.loads(f.read_text())
                if isinstance(d, dict):
                    keys = ", ".join(list(d.keys())[:8])
            except Exception:
                keys = "_parse error_"
        out.append(f"| {f.name} | {sz}b | {keys[:100]} |")
    out.append("")
    return "\n".join(out)


def section_dashboard_tiles():
    cfg = safe_yaml(DASHBOARDS / "config" / "dashboard-tiles.yaml")
    if not cfg:
        return ""
    out = ["## Dashboard tiles (from dashboard-tiles.yaml)\n"]
    tiles = cfg.get("tiles", []) if isinstance(cfg, dict) else []
    if isinstance(tiles, dict):
        tiles = list(tiles.values())
    if not tiles:
        for k, v in cfg.items():
            if isinstance(v, list):
                tiles = v
                break
    out.append("| Tile | Source | Notes |")
    out.append("|---|---|---|")
    for t in (tiles or [])[:50]:
        if isinstance(t, dict):
            n = t.get("name") or t.get("id") or ""
            s = t.get("source") or t.get("data") or ""
            note = t.get("description") or t.get("note") or ""
            out.append(f"| {n} | {s} | {note[:80]} |")
        else:
            out.append(f"| {t} | | |")
    out.append("")
    return "\n".join(out)


def section_data_flow_summary():
    return """## Data flow summary

```
GMAIL ─┐
OTTER ─┼─► cos_capture_pipeline.py ─┬─► data/deals/<deal>/log.json   ◄── deal-tagged feed
CAL   ─┘                            ├─► dashboard-data.json (compiled)
                                    └─► Follow-ups + People/CRM (Drive)

DEAL FOLDER FILES (Drive) ──► /deal-sync (every 2h, headless `claude -p`)
                                       │
                                       ├─► reads new files since last_run + new log.json entries
                                       ├─► writes {deal}_status.md (Drive)
                                       ├─► writes {deal}_master_brief.md (Drive)
                                       ├─► writes {deal}_dashboard_entry.json (Drive)
                                       └─► regenerates ACTIVE DEAL PIPELINE in the Firm Context doc

CLAUDE CODE / claude.ai DEAL-INTEL blocks ──► intel_capture ──► log.json ──► /deal-sync next cycle

REFERENCE DOCS (Drive) ─► dash-state-hook every 2h ─► ~/cos-pipeline-config-tomac/reference_docs/
                                                       │
                                                       └─► auto git commit per changed doc

REF DOCS CHANGE ─► dash-state-hook (24h cadence) ─► /refresh-project-instructions all
                                                       └─► Chrome MCP paste into 6+ deal projects on claude.ai
```

## Stop hook fire chain

1. `seconds_since_last_run < RATE_LIMIT_SECONDS` (30 min) → exit
2. `seconds_since_deal_entry_sync >= 7200` (2h) → `run_deal_entry_sync()` (Drive entry → compiled)
3. `seconds_since_deal_extract_sync >= 7200` (2h) → `run_deal_extract_sync()` (spawns headless `/deal-sync`)
4. `seconds_since_ref_doc_sync >= 7200` (2h) → `run_reference_docs_sync()` (4 ref docs Drive→git)
5. `should_run_project_inst_sync()` (24h + ref-docs-changed) → `run_project_instructions_sync()` (Chrome MCP paste)
6. Recursion guard: any spawned headless `claude -p` runs with `DEAL_SYNC_CHILD=1` so its own Stop hook returns immediately.
"""


def main():
    sections = []
    sections.append(f"# System Map\n")
    sections.append(f"**Auto-generated** by `~/dashboards/scripts/generate-system-map.py`")
    sections.append(f"**Last regenerated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    sections.append(f"")
    sections.append(f"This file is the canonical reference for how every artifact, pipeline, and slash")
    sections.append(f"command in the pipeline system fits together. **Read this BEFORE making any**")
    sections.append(f"**architectural recommendation** — it captures connections you may otherwise miss.")
    sections.append(f"")
    sections.append(f"Regenerated automatically by `dash-state-hook.py` when `drive-docs.yaml` or")
    sections.append(f"`schedule.yaml` changes. To force regenerate: `python3 ~/dashboards/scripts/generate-system-map.py`.")
    sections.append(f"")
    sections.append(section_data_flow_summary())
    sections.append(section_drive_docs())
    sections.append(section_per_deal_files())
    sections.append(section_compiled_outputs())
    sections.append(section_scheduled_tasks())
    sections.append(section_stop_hook())
    sections.append(section_slash_commands())

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(sections))
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size}b)")


if __name__ == "__main__":
    main()
