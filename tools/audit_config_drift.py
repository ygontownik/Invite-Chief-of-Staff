#!/usr/bin/env python3
"""
audit_config_drift.py
=====================

Audit the COS pipeline config-path topology for silent drift.

ARCHITECTURE
------------
Three repos:
  ~/cos-pipeline/                 — public code, no tenant data
  ~/cos-pipeline-config-tomac/    — canonical tenant config (private)
  ~/dashboards/                   — runtime data + symlinks back to tenant config

The dashboard server and pipeline scripts are supposed to read tenant
config through ~/dashboards/config/<file>, which should be a symlink
into ~/cos-pipeline-config-tomac/config/<file>. When that link is
missing — or when a real file exists on both sides — edits made in one
location become invisible to consumers reading from the other.

WHAT THIS SCRIPT CATCHES
------------------------
1. Broken or missing symlinks under ~/dashboards/config/ and
   ~/dashboards/data/ (target file does not exist on disk).

2. Drift candidates: a config file that exists in BOTH
   ~/cos-pipeline-config-tomac/config/ AND ~/dashboards/config/ as a
   real file (not a symlink). Edits made on one side will not propagate.
   Reported as DRIFT if contents also differ; STALE-COPY if identical.

3. Code that reads tenant config from ~/dashboards/config/ AND
   ~/cos-pipeline-config-tomac/ in the same Python file. This is the
   shape of the bug the script was written for: a server reads from
   one path while editors write to another.

INTERPRETING RESULTS
--------------------
  [OK]      Symlink resolves cleanly to the canonical config.
  [BROKEN]  Symlink target does not exist. Fix: re-point the link.
  [DRIFT]   Real files in both repos with different content. Pick the
            authoritative copy (almost always the cos-pipeline-config-tomac
            one), replace the dashboards copy with a symlink.
  [STALE]   Real files in both repos with identical content. Replace the
            dashboards copy with a symlink before someone edits one and
            forgets the other.
  [DUAL-READ] A Python file references both ~/dashboards/config/ AND
            ~/cos-pipeline-config-tomac/. May be intentional (fallback
            chain in _firm_context.py is a known case) — review listed
            line numbers.

Exits 0 if clean (only OK + acknowledged DUAL-READ fallbacks),
1 if any BROKEN/DRIFT/STALE found.

USAGE
-----
  python3 ~/cos-pipeline/tools/audit_config_drift.py
  python3 ~/cos-pipeline/tools/audit_config_drift.py --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
COS_PIPELINE = HOME / "cos-pipeline"
TENANT_CONFIG = HOME / "cos-pipeline-config-tomac"
DASHBOARDS = HOME / "dashboards"

# Files in cos-pipeline/_firm_context.py and similar that intentionally
# probe BOTH locations as a fallback chain. These are not drift bugs.
KNOWN_FALLBACK_FILES = {
    "_firm_context.py",         # documented fallback to ~/dashboards/config/drive-docs.yaml
    "setup.py",                 # discovers config across legacy + canonical paths
    "setup_new_firm.py",        # same
    "validate_tenant.py",       # explicit tenant validation, references both
    "deal-system-compile.py",   # documents both as input directories
    "cos-dashboard-server.py",  # documented one-release fallback for legacy tomac-config.yaml
    "audit_config_drift.py",    # this script — references both intentionally
}


@dataclass
class Findings:
    ok: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    dual_read: list[str] = field(default_factory=list)
    dual_read_acknowledged: list[str] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(self.broken or self.drift or self.stale)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def audit_symlinks(roots: list[Path], findings: Findings, verbose: bool) -> None:
    for root in roots:
        if not root.exists():
            continue
        for entry in sorted(root.rglob("*")):
            if not entry.is_symlink():
                continue
            target = entry.resolve()
            display = entry.relative_to(HOME)
            if not target.exists():
                # readlink without resolving (so we report what was intended)
                raw = entry.readlink()
                findings.broken.append(f"~/{display}  ->  {raw}  (target missing)")
            else:
                if verbose:
                    findings.ok.append(f"~/{display}  ->  {target}")
                else:
                    findings.ok.append(f"~/{display}")


def audit_drift_candidates(findings: Findings) -> None:
    """For each canonical config file, check the dashboards/config/ shadow."""
    canonical_dir = TENANT_CONFIG / "config"
    dash_dir = DASHBOARDS / "config"
    if not canonical_dir.exists() or not dash_dir.exists():
        return

    for canonical in sorted(canonical_dir.iterdir()):
        if not canonical.is_file():
            continue
        # skip backup files
        if ".bak" in canonical.name or canonical.name.endswith("~"):
            continue
        shadow = dash_dir / canonical.name
        if not shadow.exists() and not shadow.is_symlink():
            # Not exposed via dashboards. May be intentional (e.g. known-aliases.yaml
            # is read directly from the tenant repo). Skip silently.
            continue
        if shadow.is_symlink():
            # already a link — symlink audit will catch broken cases
            continue
        # real file on both sides
        try:
            same = _sha(canonical) == _sha(shadow)
        except OSError as e:
            findings.broken.append(f"~/{shadow.relative_to(HOME)}  (read failed: {e})")
            continue
        rel_can = canonical.relative_to(HOME)
        rel_sh = shadow.relative_to(HOME)
        if same:
            findings.stale.append(
                f"~/{rel_sh}  ==  ~/{rel_can}  (identical copies — replace shadow with symlink)"
            )
        else:
            findings.drift.append(
                f"~/{rel_sh}  !=  ~/{rel_can}  (DIVERGED — pick authoritative copy, then symlink)"
            )

    # Also flag top-level tenant files (e.g. drive-docs.yaml at repo root,
    # not in config/) that have a real-file shadow under dashboards/config/.
    for canonical in sorted(TENANT_CONFIG.iterdir()):
        if not canonical.is_file():
            continue
        if ".bak" in canonical.name or canonical.name.endswith("~"):
            continue
        shadow = dash_dir / canonical.name
        if not shadow.exists() or shadow.is_symlink():
            continue
        try:
            same = _sha(canonical) == _sha(shadow)
        except OSError as e:
            findings.broken.append(f"~/{shadow.relative_to(HOME)}  (read failed: {e})")
            continue
        rel_can = canonical.relative_to(HOME)
        rel_sh = shadow.relative_to(HOME)
        if same:
            findings.stale.append(
                f"~/{rel_sh}  ==  ~/{rel_can}  (identical copies — pick one canonical home)"
            )
        else:
            findings.drift.append(
                f"~/{rel_sh}  !=  ~/{rel_can}  (DIVERGED — code reading from one will miss edits to the other)"
            )


_RE_DASH_CFG = re.compile(r"dashboards/config/[^\s\"')]+")
_RE_TENANT = re.compile(r"cos-pipeline-config(?:-[a-z0-9_-]+)?(?:/[^\s\"')]*)?")


def audit_dual_read(findings: Findings, verbose: bool) -> None:
    """Find Python files that reference BOTH dashboards/config/ AND a tenant config repo."""
    for py in sorted(COS_PIPELINE.rglob("*.py")):
        # skip caches and tests
        parts = py.relative_to(COS_PIPELINE).parts
        if "__pycache__" in parts:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        dash_lines: list[int] = []
        tenant_lines: list[int] = []
        for i, line in enumerate(text.splitlines(), 1):
            # ignore comments-only mentions? no — comments still matter for
            # documenting intent, so we report them but use KNOWN_FALLBACK_FILES
            # to acknowledge.
            if _RE_DASH_CFG.search(line):
                dash_lines.append(i)
            if _RE_TENANT.search(line):
                tenant_lines.append(i)
        if dash_lines and tenant_lines:
            rel = py.relative_to(HOME)
            msg = (
                f"~/{rel}  dashboards/config refs on lines {dash_lines[:5]}"
                f"{'...' if len(dash_lines) > 5 else ''}; "
                f"tenant-config refs on lines {tenant_lines[:5]}"
                f"{'...' if len(tenant_lines) > 5 else ''}"
            )
            if py.name in KNOWN_FALLBACK_FILES:
                findings.dual_read_acknowledged.append(msg)
            else:
                findings.dual_read.append(msg)


def render(findings: Findings, verbose: bool) -> None:
    print("=" * 70)
    print("COS PIPELINE — CONFIG PATH DRIFT AUDIT")
    print("=" * 70)
    print()

    print(f"[ OK ]      {len(findings.ok)} symlinks resolve cleanly")
    if verbose:
        for line in findings.ok:
            print(f"            {line}")
    print()

    print(f"[ BROKEN ]  {len(findings.broken)}")
    for line in findings.broken:
        print(f"            {line}")
    print()

    print(f"[ DRIFT ]   {len(findings.drift)}  (real files on both sides, content differs)")
    for line in findings.drift:
        print(f"            {line}")
    print()

    print(f"[ STALE ]   {len(findings.stale)}  (real files on both sides, identical — replace shadow with symlink)")
    for line in findings.stale:
        print(f"            {line}")
    print()

    print(f"[ DUAL-READ ]  {len(findings.dual_read)}  unacknowledged Python files referencing both repos")
    for line in findings.dual_read:
        print(f"               {line}")
    print()

    if verbose and findings.dual_read_acknowledged:
        print(f"[ DUAL-READ acknowledged ]  {len(findings.dual_read_acknowledged)}  (intentional fallback chains)")
        for line in findings.dual_read_acknowledged:
            print(f"               {line}")
        print()

    print("-" * 70)
    if findings.has_problems:
        print("RESULT: DRIFT DETECTED — exit 1")
        print("Fix order:")
        print("  1. Resolve BROKEN symlinks first (re-point or remove).")
        print("  2. For each DRIFT, pick the authoritative copy (usually the")
        print("     cos-pipeline-config-tomac one), back up the other, replace")
        print("     it with a symlink.")
        print("  3. For each STALE, just replace shadow with symlink.")
    else:
        print("RESULT: CLEAN — exit 0")
    print("-" * 70)


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit COS config-path drift")
    ap.add_argument("--verbose", "-v", action="store_true", help="Show per-symlink targets and acknowledged dual-reads")
    args = ap.parse_args()

    findings = Findings()
    audit_symlinks([DASHBOARDS / "config", DASHBOARDS / "data"], findings, args.verbose)
    audit_drift_candidates(findings)
    audit_dual_read(findings, args.verbose)
    render(findings, args.verbose)
    return 1 if findings.has_problems else 0


if __name__ == "__main__":
    sys.exit(main())
