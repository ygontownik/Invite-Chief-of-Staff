#!/opt/homebrew/bin/python3
"""
naming.py — canonical filename convention for the COS local + Drive pipeline
============================================================================
Pattern:  YYYY-MM-DD_entity_doc-type_vN.ext   (all lowercase)

  - date     : ISO date. File's own modified date when organizing existing
               files (preserves chronology); today for newly authored files.
  - entity   : deal/workstream slug (e.g. acme-power, project-atlas, ...).
  - doc-type : edd, term-sheet, model, deck, memo, brief, tracker, transcript,
               nda, cim, teaser, diligence, timeline, map, note, artifact, doc.
  - vN       : version. Parsed from the source name if present, else v1,
               bumped to avoid collisions inside a destination dir.

Shared by local_file_organizer.py and local_file_router.py so local and Drive
names match. Keep dependency-free (stdlib only).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# Ordered most-specific → least. First match wins.
DOC_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(edd|enhanced[\s_-]?due[\s_-]?dilig|background[\s_-]?check)", re.I), "edd"),
    (re.compile(r"(term[\s_-]?sheet|termsheet|letter[\s_-]?of[\s_-]?intent|\bloi\b)", re.I), "term-sheet"),
    (re.compile(r"(non[\s_-]?disclosure|confidentiality[\s_-]?agreement|\bnda\b|joinder)", re.I), "nda"),
    (re.compile(r"(\bcim\b|conf(?:idential)?[\s_-]?info(?:rmation)?[\s_-]?mem)", re.I), "cim"),
    (re.compile(r"(teaser|one[\s_-]?pager|onepager|opportunity[\s_-]?overview)", re.I), "teaser"),
    (re.compile(r"(\blbo\b|\bdcf\b|waterfall|\bmodel\b|projection|returns)", re.I), "model"),
    (re.compile(r"(deck|pitch|presentation|investor[\s_-]?overview|\boverview\b|discussion[\s_-]?materials|slides)", re.I), "deck"),
    (re.compile(r"(memo|memorandum)", re.I), "memo"),
    (re.compile(r"(brief|briefing)", re.I), "brief"),
    (re.compile(r"(tracker|universe|outreach[\s_-]?list)", re.I), "tracker"),
    (re.compile(r"(transcript|call[\s_-]?notes|meeting[\s_-]?notes|call[\s_-]?summary)", re.I), "transcript"),
    (re.compile(r"(diligence|document[\s_-]?requests?|doc[\s_-]?requests?|checklist|data[\s_-]?room|schedule)", re.I), "diligence"),
    (re.compile(r"(timeline|process[\s_-]?timeline|roadmap)", re.I), "timeline"),
    (re.compile(r"(\bmap\b|corridor)", re.I), "map"),
]

EXT_DEFAULT_TYPE = {
    ".xlsx": "model", ".xlsm": "model", ".xls": "model",
    ".pptx": "deck", ".ppt": "deck", ".key": "deck",
    ".jsx": "artifact", ".tsx": "artifact", ".html": "artifact",
    ".md": "note", ".txt": "note",
}

# v6, _v6, " v6" — but NOT v2026 (4+ digits) or v2026.06 (date-like).
_VERSION_RE = re.compile(r"(?:^|[_\s.-])v(\d{1,3})(?![\d.])", re.I)


def slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


# For legal docs, status matters as much as type — never lose which copy is
# signed. Appended as a hyphenated qualifier, e.g. nda-executed, term-sheet-redline.
STATUS_QUALIFIERS = [
    (re.compile(r"(executed|countersigned|fully[\s]?signed|\bsigned\b)", re.I), "executed"),
    (re.compile(r"(redline|redlined|markup|marked[\s]?up)", re.I), "redline"),
    (re.compile(r"\bdraft\b", re.I), "draft"),
]
QUALIFY_TYPES = {"nda", "term-sheet", "cim", "loi"}


def detect_doc_type(filename: str) -> str:
    # Normalize separators (_ . -) to spaces so \b boundaries work around
    # tokens like "_NDA_" and "Project250_NDA".
    stem = re.sub(r"[._-]+", " ", Path(filename).stem)
    base = EXT_DEFAULT_TYPE.get(Path(filename).suffix.lower(), "doc")
    for rx, label in DOC_TYPE_RULES:
        if rx.search(stem):
            base = label
            break
    if base in QUALIFY_TYPES:
        for rx, q in STATUS_QUALIFIERS:
            if rx.search(stem):
                return f"{base}-{q}"
    return base


def detect_version(filename: str) -> int | None:
    m = _VERSION_RE.search(Path(filename).stem)
    return int(m.group(1)) if m else None


def date_str_for(src_path: Path | None = None, *, today: str | None = None) -> str:
    if today:
        return today
    if src_path is not None:
        try:
            return datetime.fromtimestamp(Path(src_path).stat().st_mtime).strftime("%Y-%m-%d")
        except OSError:
            pass
    return datetime.now().strftime("%Y-%m-%d")


def convention_name(
    original_name: str,
    entity: str,
    *,
    date: str | None = None,
    src_path: Path | None = None,
    dst_dir: Path | None = None,
) -> str:
    """Return YYYY-MM-DD_entity_doc-type_vN.ext.

    If dst_dir is a local directory, the version is bumped until the name is
    collision-free there. For Drive uploads, pass dst_dir=None.
    """
    ext = Path(original_name).suffix.lower()
    d = date or date_str_for(src_path)
    ent = slugify(entity) or "misc"
    dtype = detect_doc_type(original_name)
    ver = detect_version(original_name) or 1

    def build(v: int) -> str:
        return f"{d}_{ent}_{dtype}_v{v}{ext}"

    name = build(ver)
    if dst_dir is not None:
        dd = Path(dst_dir)
        while (dd / name).exists():
            ver += 1
            name = build(ver)
    return name


if __name__ == "__main__":
    # Smoke test
    samples = [
        ("StCroix_Model_v3.xlsx", "acme-power"),
        ("Project_NDA_Redlined_v6.docx", "acme-power"),
        ("project_teaser_onepager (1).pdf", "acme-power"),
        ("Consultant_EDD_Tier1_Report_v3.md", "acme-power"),
        ("GP_Economics_v5.pptx", "project-atlas"),
        ("Investor_Overview.pdf", "project-atlas"),
        ("Bid_Comparison_6.7.2026.pdf", "project-x"),
        ("Discussion_Materials_v2026.06.10.pdf", "acme-power"),
    ]
    for n, e in samples:
        print(f"{n!r:60} -> {convention_name(n, e, date='2026-06-11')}")
