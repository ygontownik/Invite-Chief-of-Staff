"""refresh_bundle.py — auto-refresh the Active deal themes block in system_prompt_v1.md.

Reads the live deal-pipeline-data.json, picks the top N themes by themeScore
(filtered to High/Medium conviction by default), and rewrites the block
delimited by `<!-- AUTO_THEMES_BEGIN -->` / `<!-- AUTO_THEMES_END -->`.

Idempotent: rerunning with no upstream changes produces no diff. Atomic:
writes via temp file + rename. Cron-safe: returns nonzero on hard failure
so a wrapper can alert.

Usage:
    python3 refresh_bundle.py            # default: top 7, High+Medium conviction
    python3 refresh_bundle.py --top 5    # smaller bundle
    python3 refresh_bundle.py --dry-run  # show what would change, don't write
    python3 refresh_bundle.py --all-conviction  # include Low-conviction themes
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

_HERE = pathlib.Path(__file__).resolve().parent
_PROMPT_FILE = _HERE / "system_prompt_v1.md"
_PIPELINE_DATA = pathlib.Path(os.environ.get("COS_DEAL_DATA", "")) or (pathlib.Path.home() / "cos-pipeline" / "data" / "compiled" / "deal-pipeline-data.json")

_BEGIN = "<!-- AUTO_THEMES_BEGIN — managed by _subscription/refresh_bundle.py; do not hand-edit -->"
_END = "<!-- AUTO_THEMES_END -->"

_CONVICTION_RANK = {"High": 3, "Medium": 2, "Low": 1, "Unknown": 0}


def _load_themes() -> list[dict]:
    if not _PIPELINE_DATA.exists():
        raise FileNotFoundError(f"deal-pipeline-data.json not found at {_PIPELINE_DATA}")
    data = json.loads(_PIPELINE_DATA.read_text(encoding="utf-8"))
    return data.get("themes", [])


def _select_top(themes: list[dict], top_n: int, allow_low: bool) -> list[dict]:
    """Sort by (themeScore desc, conviction rank desc) and take top_n."""
    filtered = [
        t for t in themes
        if allow_low or _CONVICTION_RANK.get(t.get("conviction", "Unknown"), 0) >= 2
    ]
    filtered.sort(
        key=lambda t: (
            -int(t.get("themeScore", 0)),
            -_CONVICTION_RANK.get(t.get("conviction", "Unknown"), 0),
        )
    )
    return filtered[:top_n]


def _format_block(themes: list[dict], top_n: int) -> str:
    """Render the Active deal themes block, including the begin/end sentinels."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"### Active deal themes (top {len(themes)} by score, week of {today})"
    lines = [_BEGIN, header, ""]
    for t in themes:
        thesis_preview = (t.get("thesis", "") or "")[:200].rstrip()
        theme_name = t.get("theme", t.get("id", "(unnamed)"))
        lines.append(f"- **[{t.get('id')}] {theme_name}** — {thesis_preview}")
    lines.append(_END)
    return "\n".join(lines)


def _splice_in(prompt_text: str, new_block: str) -> str:
    """Replace the existing AUTO_THEMES block. Raise if sentinels missing."""
    if _BEGIN not in prompt_text or _END not in prompt_text:
        raise RuntimeError(
            f"system_prompt_v1.md missing sentinel(s). "
            f"Expected both:\n  {_BEGIN}\n  {_END}"
        )
    pre, rest = prompt_text.split(_BEGIN, 1)
    _, post = rest.split(_END, 1)
    return pre + new_block + post


def _atomic_write(path: pathlib.Path, content: str) -> None:
    """Write to a sibling tempfile then os.replace into place."""
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=7, help="Number of themes to include (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print proposed block; do not write")
    parser.add_argument("--all-conviction", action="store_true", help="Include Low-conviction themes")
    args = parser.parse_args()

    themes = _load_themes()
    if not themes:
        print("ERROR: deal-pipeline-data.json contains no themes; refusing to overwrite", file=sys.stderr)
        return 2

    selected = _select_top(themes, args.top, args.all_conviction)
    if not selected:
        print("ERROR: no themes passed the conviction filter; refusing to overwrite", file=sys.stderr)
        return 2

    new_block = _format_block(selected, args.top)

    current = _PROMPT_FILE.read_text(encoding="utf-8")
    proposed = _splice_in(current, new_block)

    if proposed == current:
        print(f"refresh_bundle: no change (top {len(selected)} themes already current)")
        return 0

    if args.dry_run:
        print("=== PROPOSED BLOCK ===")
        print(new_block)
        print()
        print("=== DIFF SUMMARY ===")
        print(f"  current bytes : {len(current)}")
        print(f"  proposed bytes: {len(proposed)}")
        return 0

    _atomic_write(_PROMPT_FILE, proposed)
    print(f"refresh_bundle: wrote {_PROMPT_FILE} with top {len(selected)} themes")
    print(f"  ids: {', '.join(t['id'] for t in selected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
