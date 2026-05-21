#!/usr/bin/env python3
"""
sync_registry.py — Regenerate downstream registries from drive-docs.yaml.

`drive-docs.yaml` (under cos-pipeline-config-<tenant>/) is the canonical Drive
ID + deal-routing registry. This script reads it and regenerates the matching
sections in:

  1. tc_config.gs — TC Config GAS library getDeals() body
  2. drive_organizer.gs — DEAL_FOLDERS object
  3. (planned) local_file_router.py — DEALS regex dict
  4. (planned) deal-system-data.json — compact derived view

All edits target sections between sentinel markers:
    // ─── BEGIN GENERATED FROM drive-docs.yaml — DO NOT EDIT MANUALLY ───
    ...
    // ─── END GENERATED ───
Manual code outside markers is preserved.

Usage:
    python3 sync_registry.py                  # dry run, show diff
    python3 sync_registry.py --apply          # write changes locally
    python3 sync_registry.py --apply --push   # write + clasp push

Multi-tenant: reads tenant config via $COS_CONFIG_DIR or glob discovery
(~/cos-pipeline-config-*/drive-docs.yaml). Public-repo safe.
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import yaml

# ── coordination layer ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from coordination import lock, mark_run  # noqa: E402

HOLDER = "sync_registry.py"

# ── paths (tenant-discovered, never hardcoded slugs in this file) ────────────

def find_drive_docs() -> Path:
    """Find drive-docs.yaml via env var or glob. Multi-tenant safe."""
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "drive-docs.yaml"
        if p.exists():
            return p
    candidates = sorted(glob.glob(str(Path.home() / "cos-pipeline-config-*/drive-docs.yaml")))
    if not candidates:
        sys.exit("ERROR: no drive-docs.yaml found. Set $COS_CONFIG_DIR or "
                 "place at ~/cos-pipeline-config-<tenant>/drive-docs.yaml")
    return Path(candidates[0])


DRIVE_DOCS = find_drive_docs()
GAS_DIR = Path.home() / "dashboards/routines/gas"
TC_CONFIG_GS = GAS_DIR / "tc-config/tc_config.gs"
DRIVE_ORG_GS = GAS_DIR / "drive-organizer/drive_organizer.gs"

BEGIN_MARKER = "// ─── BEGIN GENERATED FROM drive-docs.yaml — DO NOT EDIT MANUALLY ───"
END_MARKER = "// ─── END GENERATED ───"

# ── helpers ──────────────────────────────────────────────────────────────────

def js_str(s: str) -> str:
    """Quote a string for JavaScript output, single-quoted."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def js_array(items: Iterable[str]) -> str:
    """Render a list of strings as a JS array."""
    return "[" + ",".join(js_str(x) for x in items) + "]"


def replace_between_markers(text: str, begin: str, end: str, new_body: str) -> str:
    """Replace whatever lives between BEGIN and END markers with new_body.
    Preserves the markers themselves. Raises if markers are missing."""
    pattern = re.compile(
        re.escape(begin) + r"(.*?)" + re.escape(end),
        flags=re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError(f"Markers not found in target file. Expected {begin!r} ... {end!r}")
    return pattern.sub(begin + "\n" + new_body + "\n    " + end, text)


# ── rendering ────────────────────────────────────────────────────────────────

def render_tc_config_deals(deal_docs: dict, pipeline_deals: dict) -> str:
    """Render the getDeals() body. Tier-grouped (I → II → III).

    Each entry in tc_config.gs has the shape:
        { name: <display>,
          tier: 'I' | 'II' | 'III',
          lead: <2-letter>, sector: <name>,
          driveId: D.<GAS_CONST> | null,
          routingAlias: <display>,
          subfolders: { ... },                  // optional
          keywords: [...],
          counterparties: [...],
        },

    The driveId reference comes from each deal's `gas_const:` field in
    drive-docs.yaml (tenant-defined). Public-repo safe — no hardcoded slugs.
    """
    lines = []

    # Tier I — active deals from deal_docs (have drive_folder_id + full scaffold)
    lines.append("\n    // ── TIER I — ACTIVE ────────────────────────────────────────────────────\n")
    for deal_id, entry in deal_docs.items():
        if entry.get("tier", "I") != "I":
            continue
        name = entry.get("name", deal_id)
        lead = entry.get("lead", "")
        sector = entry.get("sector", "")
        # gas_const field in drive-docs.yaml names the F.* constant in
        # drive_organizer.gs that holds this deal's Drive folder ID.
        drive_const = entry.get("gas_const")
        drive_ref = f"D.{drive_const}" if drive_const else "null"
        lines.append(f"    {{ name: {js_str(name)},")
        lines.append(f"      tier: {js_str(entry.get('tier','I'))}, "
                     f"lead: {js_str(lead)}, sector: {js_str(sector)},")
        lines.append(f"      driveId: {drive_ref},")
        lines.append(f"      routingAlias: {js_str(name)},")
        lines.append(f"      keywords: {js_array(entry.get('keywords', []))},")
        lines.append(f"      counterparties: {js_array(entry.get('counterparties', []))},")
        lines.append("    },\n")

    # Tier II + III — pipeline_deals
    for tier in ("II", "III"):
        header = "TIER II — ACTIVE PIPELINE" if tier == "II" else "TIER III — WATCH LIST"
        lines.append(f"    // ── {header} ──────────────────────────────────────\n")
        for deal_id, entry in pipeline_deals.items():
            if entry.get("tier") != tier:
                continue
            lines.append(f"    {{ name: {js_str(entry.get('name', deal_id))},")
            lines.append(f"      tier: {js_str(tier)}, "
                         f"lead: {js_str(entry.get('lead',''))}, "
                         f"sector: {js_str(entry.get('sector',''))},")
            lines.append("      driveId: null,")
            lines.append(f"      keywords: {js_array(entry.get('keywords', []))},")
            lines.append(f"      counterparties: {js_array(entry.get('counterparties', []))},")
            lines.append("    },\n")

    return "".join(lines).rstrip() + "\n"


def render_drive_org_deal_folders(deal_docs: dict) -> str:
    """Render the DEAL_FOLDERS object body. Maps F.<DEAL> → display name.

    Each deal's `gas_const:` field in drive-docs.yaml names the F.* constant.
    Public-repo safe — no hardcoded tenant slugs.

    Legacy F constants for retired deals (Hawthorne, GTC Towers, Fiber) are
    preserved in tc_config.gs `const F` and rendered here so the Drive Organizer
    Auditor still walks them.
    """
    lines = ["const DEAL_FOLDERS = {"]
    # Tier I active deals from drive-docs.yaml
    for deal_id, entry in deal_docs.items():
        const = entry.get("gas_const")
        if not const:
            continue
        display = entry.get("name", deal_id)
        lines.append(f"  [F.{const}]:".ljust(20) + f"{js_str(display)},")
    # Legacy entries (retired deals still in F for the auditor to scan)
    for legacy_const, legacy_name in (("HAWTHORNE","Hawthorne"), ("GTC_TOWERS","GTC Towers"), ("FIBER","Fiber")):
        lines.append(f"  [F.{legacy_const}]:".ljust(20) + f"{js_str(legacy_name)},")
    lines.append("};")
    return "\n".join(lines)


# ── apply ────────────────────────────────────────────────────────────────────

def apply_to_tc_config(deal_docs: dict, pipeline_deals: dict, dry_run: bool) -> bool:
    text = TC_CONFIG_GS.read_text()
    body = render_tc_config_deals(deal_docs, pipeline_deals)
    new_text = replace_between_markers(text, BEGIN_MARKER, END_MARKER, body)
    if new_text == text:
        print(f"  tc_config.gs:        no change")
        return False
    if dry_run:
        print(f"  tc_config.gs:        WOULD UPDATE ({len(text)} → {len(new_text)} chars)")
    else:
        TC_CONFIG_GS.write_text(new_text)
        print(f"  tc_config.gs:        UPDATED ({len(text)} → {len(new_text)} chars)")
    return True


def apply_to_drive_organizer(deal_docs: dict, dry_run: bool) -> bool:
    text = DRIVE_ORG_GS.read_text()
    new_body = render_drive_org_deal_folders(deal_docs)
    # Replace the entire `const DEAL_FOLDERS = { ... };` block between sentinels
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r".*?const DEAL_FOLDERS = \{.*?\};.*?" + re.escape(END_MARKER),
        flags=re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError("drive_organizer.gs missing DEAL_FOLDERS sentinel block")
    replacement = (
        BEGIN_MARKER + "\n"
        "// Regenerated by ~/cos-pipeline/tools/sync_registry.py.\n"
        + new_body + "\n"
        + END_MARKER
    )
    new_text = pattern.sub(replacement, text)
    if new_text == text:
        print(f"  drive_organizer.gs:  no change")
        return False
    if dry_run:
        print(f"  drive_organizer.gs:  WOULD UPDATE")
    else:
        DRIVE_ORG_GS.write_text(new_text)
        print(f"  drive_organizer.gs:  UPDATED")
    return True


def clasp_push(project_dir: Path) -> bool:
    """Run `clasp push -f` from a clasp project dir."""
    try:
        result = subprocess.run(
            ["clasp", "push", "-f"],
            cwd=str(project_dir),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"  clasp push {project_dir.name}:  ✓")
            return True
        print(f"  clasp push {project_dir.name}:  FAIL\n{result.stderr}")
        return False
    except Exception as e:
        print(f"  clasp push {project_dir.name}:  ERROR {e}")
        return False


# ── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    p.add_argument("--push", action="store_true",
                   help="After --apply, run `clasp push` to deploy GAS changes")
    args = p.parse_args(argv)

    print(f"sync_registry.py — source: {DRIVE_DOCS}")
    docs = yaml.safe_load(DRIVE_DOCS.read_text())
    deal_docs = docs.get("deal_docs") or {}
    pipeline_deals = docs.get("pipeline_deals") or {}
    print(f"  loaded {len(deal_docs)} deal_docs + {len(pipeline_deals)} pipeline_deals")

    dry = not args.apply

    with lock("drive-docs.yaml", HOLDER, ttl_seconds=180, timeout_seconds=60):
        changed_tc = apply_to_tc_config(deal_docs, pipeline_deals, dry_run=dry)
        changed_org = apply_to_drive_organizer(deal_docs, dry_run=dry)

    if args.apply and args.push:
        print()
        if changed_tc:
            with lock("gas:tc-config", HOLDER, ttl_seconds=120, timeout_seconds=60):
                clasp_push(GAS_DIR / "tc-config")
        if changed_org:
            with lock("gas:drive-organizer", HOLDER, ttl_seconds=120, timeout_seconds=60):
                clasp_push(GAS_DIR / "drive-organizer")

    if args.apply:
        mark_run(HOLDER)

    print()
    print("Done." if not dry else "Dry run complete. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
