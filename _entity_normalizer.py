#!/usr/bin/env python3
"""
_entity_normalizer.py — Reconcile extracted person/firm strings against canonical roster.

Audio-transcription engines (Otter, Zoom AI, Teams) consistently mis-hear known
names — e.g. "<FirmA>" gets transcribed as "<homophone>". Without reconciliation,
errors propagate into the dashboard with full metadata (urgent flags, due dates,
dashboard paths) that masks the underlying mistake. This module is the single
normalization pass that every transcript / email / research extractor must run
BEFORE writing.

Loaded once per pipeline run; canonical roster is cached. Pure functions — no
side effects on dashboard state.

Usage (importable):
    from _entity_normalizer import EntityNormalizer
    norm = EntityNormalizer()
    fixed_text = norm.apply_phonetic(transcript_body)
    match = norm.match("<deal-name> Petro")
    # match → ResolvedEntity(canonical='<deal-name> / <principal>', source='deals',
    #                        confidence='substring', original='<deal-name> Petro')

Usage (CLI — for the otter-transcripts SKILL flow):
    python3 -m routines.process._entity_normalizer --check "<homophone>"
    python3 -m routines.process._entity_normalizer --check-all <<<'["<homophone>","<deal-name> Petro"]'
    python3 -m routines.process._entity_normalizer --apply-text < transcript.txt
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

# ── Paths ─────────────────────────────────────────────────────────────────────
PHONETIC_PATH        = Path.home() / "credentials/phonetic_corrections.json"
DASHBOARD_DATA_PATH  = Path.home() / "dashboards/data/compiled/dashboard-data.json"
DEAL_SYSTEM_PATH     = Path.home() / "dashboards/data/compiled/deal-system-data.json"
DEAL_PIPELINE_PATH   = Path.home() / "dashboards/data/compiled/deal-pipeline-data.json"

# ── Vague-descriptor patterns (no proper name) ────────────────────────────────
# Matches phrases like "<owner>'s buddy at X", "the X guy", "the X father figure".
# Whole-string match against an extracted Who/counterparty field.
#
# B4 (ID excision): the first pattern was hardcoded `yoni|mark|nik`. Now
# constructed from firm_context.yaml :: owner_whitelist + team[].name +
# principal.name so the regex tracks each tenant's roster automatically.
def _build_owner_possessive_pattern():
    try:
        from pathlib import Path as _P
        import sys as _sys
        _sys.path.insert(0, str(_P(__file__).resolve().parent))
        import _firm_context as _fc  # noqa: E402
        ctx = _fc.load_firm_context()
    except Exception:
        ctx = {}

    names = set()
    for n in ctx.get("owner_whitelist", []) or []:
        if n:
            names.add(str(n).strip().split()[0].lower())
    p = (ctx.get("principal", {}) or {}).get("name", "")
    if p:
        names.add(p.split()[0].lower())
    for m in ctx.get("team", []) or []:
        n = (m or {}).get("name", "")
        if n:
            names.add(n.split()[0].lower())
    names.discard("")

    alt = "|".join(sorted(re.escape(n) for n in names)) if names else r"\w+"
    return re.compile(
        rf"^({alt})'?s?\s+(buddy|friend|contact|guy|colleague)",
        re.I,
    )

VAGUE_PATTERNS = [
    _build_owner_possessive_pattern(),
    re.compile(r"^the\s+\w+\s+(guy|gal|person|contact|advisor|father\s+figure)", re.I),
    re.compile(r"^(town|local|industry)\s+\w+\s+guy", re.I),
    re.compile(r"contact\s*\(\s*identity\s+unconfirmed", re.I),
    re.compile(r"\bunknown\s+speaker\b", re.I),
    re.compile(r"^\s*speaker\s*\d+\s*$", re.I),  # raw "Speaker N" — hard error per SKILL
]


@dataclass
class ResolvedEntity:
    """A normalization result. Always carries the original string for audit."""
    original: str
    canonical: str
    source: str           # 'phonetic' | 'lp' | 'deal' | 'pipeline_target' | 'unmatched'
    confidence: str       # 'exact' | 'substring' | 'levenshtein' | 'none'
    distance: int = 0     # Levenshtein distance (0 if exact/substring)


def _levenshtein(a: str, b: str) -> int:
    """Cheap Levenshtein. Strings <= 60 chars; 2D DP fine."""
    if a == b: return 0
    if not a: return len(b)
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j-1] + 1, prev[j-1] + (ca != cb)))
        prev = curr
    return prev[-1]


class EntityNormalizer:
    """Loads canonical roster + phonetic dictionary once. Reuse across a run."""

    def __init__(self, levenshtein_max: int = 2):
        self.levenshtein_max = levenshtein_max
        self._load_phonetic()
        self._load_canonical()

    # ── Load ──────────────────────────────────────────────────────────────────
    def _load_phonetic(self) -> None:
        try:
            data = json.loads(PHONETIC_PATH.read_text())
            self.phonetic = data.get("corrections", {})
            self.person_canonicals = data.get("person_canonicals", {})
            self.levenshtein_max = data.get("review_threshold", {}).get(
                "levenshtein_max", self.levenshtein_max
            )
        except FileNotFoundError:
            self.phonetic, self.person_canonicals = {}, {}

    def _load_canonical(self) -> None:
        """Build a flat list of (canonical_name, source) tuples for matching."""
        self.canonical: list[tuple[str, str]] = []
        try:
            dash = json.loads(DASHBOARD_DATA_PATH.read_text())
            for lp in dash.get("lpData", []):
                if name := lp.get("name"): self.canonical.append((name, "lp"))
        except FileNotFoundError: pass
        try:
            ds = json.loads(DEAL_SYSTEM_PATH.read_text())
            for d in ds.get("deals", []):
                if name := d.get("name"): self.canonical.append((name, "deal"))
        except FileNotFoundError: pass
        try:
            dp = json.loads(DEAL_PIPELINE_PATH.read_text())
            for theme in dp.get("themes", []):
                for tgt in theme.get("targets", []):
                    if name := tgt.get("name"):
                        self.canonical.append((name, "pipeline_target"))
        except FileNotFoundError: pass

    # ── Public API ────────────────────────────────────────────────────────────
    def apply_phonetic(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Whole-word substitution from the phonetic dictionary.

        Returns (corrected_text, [(misheard, canonical), ...]) so callers can log.
        """
        applied: list[tuple[str, str]] = []
        for misheard, canonical in self.phonetic.items():
            pattern = re.compile(rf"\b{re.escape(misheard)}\b", re.IGNORECASE)
            new_text, n = pattern.subn(canonical, text)
            if n:
                applied.append((misheard, canonical))
                text = new_text
        return text, applied

    def match(self, s: str) -> ResolvedEntity:
        """Reconcile a single extracted entity string against canonical roster."""
        if not s or not s.strip():
            return ResolvedEntity(s, s, "unmatched", "none")
        s_clean = s.strip()
        s_lower = s_clean.lower()

        # 1. Phonetic dictionary first (already-substituted text usually skips here,
        #    but for entity-by-entity calls we still check).
        if s_clean in self.phonetic:
            return ResolvedEntity(s_clean, self.phonetic[s_clean], "phonetic", "exact")
        for k, v in self.phonetic.items():
            if k.lower() == s_lower:
                return ResolvedEntity(s_clean, v, "phonetic", "exact")

        # 2. Exact match against canonical roster (case-insensitive).
        for name, source in self.canonical:
            if name.lower() == s_lower:
                return ResolvedEntity(s_clean, name, source, "exact")

        # 3. Substring (s in canonical or canonical in s) — handles "AlphaCo Petro"
        #    inside "AlphaCo / Jane Doe".
        for name, source in self.canonical:
            n_lower = name.lower()
            if (s_lower in n_lower) or any(tok and tok in s_lower for tok in n_lower.split(" / ")):
                return ResolvedEntity(s_clean, name, source, "substring")

        # 4. Levenshtein on the first whitespace-separated token vs each canonical
        #    first token (cheap and catches "Reinova" vs "Raynova").
        s_first = s_lower.split()[0] if s_lower.split() else s_lower
        for name, source in self.canonical:
            n_first = name.lower().split()[0]
            d = _levenshtein(s_first, n_first)
            if d <= self.levenshtein_max:
                return ResolvedEntity(s_clean, name, source, "levenshtein", d)

        return ResolvedEntity(s_clean, s_clean, "unmatched", "none")

    @staticmethod
    def is_vague(s: str) -> bool:
        """True if `s` is a colloquial descriptor with no proper name."""
        if not s: return False
        return any(p.search(s) for p in VAGUE_PATTERNS)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", help="Single string to reconcile.")
    g.add_argument("--check-all", action="store_true",
                   help="Read JSON list of strings from stdin, emit JSON list of resolutions.")
    g.add_argument("--apply-text", action="store_true",
                   help="Read text from stdin, emit phonetic-corrected text + JSON report.")
    args = ap.parse_args()
    norm = EntityNormalizer()

    if args.check:
        result = norm.match(args.check)
        result_dict = asdict(result)
        result_dict["is_vague"] = norm.is_vague(args.check)
        print(json.dumps(result_dict, ensure_ascii=False))
    elif args.check_all:
        items = json.loads(sys.stdin.read())
        out = [{**asdict(norm.match(s)), "is_vague": norm.is_vague(s)} for s in items]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.apply_text:
        text = sys.stdin.read()
        new_text, applied = norm.apply_phonetic(text)
        sys.stderr.write(json.dumps({"phonetic_corrections_applied": applied}) + "\n")
        sys.stdout.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
