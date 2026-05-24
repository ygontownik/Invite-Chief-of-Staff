"""cos_followup_sweep.py — FU1/FU2/FU3 follow-up staleness sweep.

Runs daily against the Drive Follow-ups doc to auto-resolve rows that are
clearly stale, duplicate, or superseded by active deal engagement. Three
sweep passes:

  FU1 — Staleness: one-time action rows (send/text/email/call/check/decide)
         >30 days past due are auto-marked [RESOLVED — stale, window passed].

  FU2 — Deal stage promotion: rows for deals with ≥10 log entries where the
         row's action is an early-stage task (send teaser, schedule first
         meeting, receive FEA) and the row is >14 days past due.

  FU3 — Deduplication: rows where normalize(who[:30] + what[:40]) matches
         an earlier open row. Keeps the earlier row; marks duplicates resolved.

Each resolved row gets a [RESOLVED YYYY-MM-DD — <short reason>] tag prepended
to its "what" cell via Google Docs API replaceAllText.

Schedule: called from cos-capture-pipeline.py daily after followups are
fetched. Also safe to run standalone:

  python3 ~/cos-pipeline/cos_followup_sweep.py [--dry-run] [--verbose]

Exit 0 always (sweep failures are non-fatal; the source doc is unchanged on
error).
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
FOLLOWUPS_DOC_ID = "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY"
DEALS_DIR = HOME / "dashboards" / "data" / "deals"
CREDS_PATH = HOME / "credentials" / "gdrive_token.pickle"

# FU1: one-time action verbs whose window expires at 30d past due
_ONE_TIME_RE = re.compile(
    r"^(send |text |email |notify |forward |provide |submit |decide |make intro|"
    r"check if|register |draft |reach out|call |grab |reply |propose |schedule |"
    r"follow up|await|identify |find |connect |message |introduce |engage |loop |"
    r"create |build |prepare |write |review |get |obtain |confirm |pull |\[overdue\])",
    re.IGNORECASE,
)

# FU1 threshold (days past due before auto-close)
_FU1_DAYS = 30

# FU2: early-stage deal action patterns (generic — no tenant-specific names)
_EARLY_STAGE_RE = re.compile(
    r"(send teaser|send full|receive teaser|receive full|schedule initial|"
    r"propose dates|arrange zoom|loop.*into nda|execute nda|"
    r"review teaser|send context|get more detailed|obtain outcome|"
    r"provide more detailed|review autonomous|contact reservoir|"
    r"follow up to get pitch|have preliminary calls|sequence preliminary|"
    r"post lc self.?funded|receive.*fea|send.*nda|send.*one.?pager to )",
    re.IGNORECASE,
)

# FU2: minimum deal log entries to consider a deal "actively engaged"
_FU2_MIN_LOG_ENTRIES = 10
_FU2_MIN_DAYS_PAST = 14

# FU3: normalization for dedup hash
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _parse_date(s: str):
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]:
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def _load_deal_log_counts() -> dict[str, int]:
    counts = {}
    if not DEALS_DIR.exists():
        return counts
    for slug_dir in DEALS_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        log_path = slug_dir / "log.json"
        if log_path.exists():
            try:
                log = json.loads(log_path.read_text())
                counts[slug_dir.name] = len(log.get("entries", []))
            except Exception:
                pass
    return counts


# Deal keyword → slug mapping for FU2 deal association.
# Loaded dynamically from tenant drive-docs.yaml (pipeline_deals[].keywords)
# so the public script has no hardcoded tenant slugs (PD1).
_DEAL_KEYWORDS: dict[str, list[str]] = {}  # populated by _load_deal_keywords()


def _load_deal_keywords() -> dict[str, list[str]]:
    """Load deal keyword lists from tenant drive-docs.yaml (private config).

    Falls back to scanning ~/dashboards/data/deals/<slug>/deal.md name lines
    if drive-docs.yaml is unavailable. Either way: no hardcoded deal names
    in this public script (PD1).
    """
    kw_map: dict[str, list[str]] = {}

    # Primary: tenant drive-docs.yaml pipeline_deals section
    tenant_cfg_dirs = sorted(HOME.glob("cos-pipeline-config-*/"))
    for cfg_dir in tenant_cfg_dirs:
        dd_yaml = cfg_dir / "drive-docs.yaml"
        if not dd_yaml.exists():
            continue
        try:
            import yaml as _yaml  # type: ignore
            doc = _yaml.safe_load(dd_yaml.read_text()) or {}
            for slug, cfg in (doc.get("pipeline_deals") or {}).items():
                kws = cfg.get("keywords") or []
                if isinstance(kws, list) and kws:
                    kw_map.setdefault(slug, []).extend(str(k).lower() for k in kws)
            if kw_map:
                return kw_map
        except Exception:
            pass

    # Fallback: read deal.md first line for deal name as keyword
    if DEALS_DIR.exists():
        for slug_dir in DEALS_DIR.iterdir():
            if not slug_dir.is_dir():
                continue
            deal_md = slug_dir / "deal.md"
            if deal_md.exists():
                try:
                    first_line = deal_md.read_text().splitlines()[0]
                    # "# Deal Name" → "deal name"
                    name = re.sub(r"^#+\s*", "", first_line).strip().lower()
                    if name:
                        kw_map[slug_dir.name] = [name, slug_dir.name.replace("_", " ")]
                except Exception:
                    pass

    return kw_map


def _deal_slug_for_row(who: str, what: str) -> str | None:
    global _DEAL_KEYWORDS
    if not _DEAL_KEYWORDS:
        _DEAL_KEYWORDS = _load_deal_keywords()
    combined = f"{who} {what}".lower()
    for slug, kws in _DEAL_KEYWORDS.items():
        if any(kw in combined for kw in kws):
            return slug
    return None


def _get_docs_service():
    if not CREDS_PATH.exists():
        raise FileNotFoundError(f"Google token not found: {CREDS_PATH}")
    try:
        from googleapiclient.discovery import build  # type: ignore
        with open(CREDS_PATH, "rb") as f:
            creds = pickle.load(f)
        return build("docs", "v1", credentials=creds)
    except ImportError:
        raise RuntimeError("google-api-python-client not installed")


def _fetch_open_rows(docs_svc) -> list[dict]:
    """Fetch the Follow-ups doc and parse open (non-resolved) pipe-table rows."""
    doc = docs_svc.documents().get(documentId=FOLLOWUPS_DOC_ID).execute()
    body = doc.get("body", {}).get("content", [])
    rows = []
    for el in body:
        if "paragraph" not in el:
            continue
        txt = "".join(
            e.get("textRun", {}).get("content", "")
            for e in el["paragraph"].get("elements", [])
        ).strip()
        if not txt.startswith("|") or txt.startswith("|---|") or txt.startswith("| #"):
            continue
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        if len(parts) < 3:
            continue
        if "[resolved" in txt.lower():
            continue
        rows.append({
            "num":  parts[0],
            "who":  parts[1] if len(parts) > 1 else "",
            "what": parts[2] if len(parts) > 2 else "",
            "due":  parts[3] if len(parts) > 3 else "",
            "ws":   parts[4] if len(parts) > 4 else "",
            "raw":  txt,
            "dd":   _parse_date(parts[3] if len(parts) > 3 else ""),
        })
    return rows


def _build_requests(rows: list[dict], log_counts: dict[str, int],
                    today, verbose: bool) -> list[tuple[str, str, str]]:
    """Return list of (old_snippet, new_snippet, reason) for rows to close."""
    today_str = today.strftime("%Y-%m-%d")
    to_close: dict[str, tuple[dict, str]] = {}  # num → (row, reason)

    # ── FU3: duplicates ──────────────────────────────────────────────────────
    seen: dict[str, dict] = {}
    for r in rows:
        h = _norm(r["who"][:30]) + "||" + _norm(r["what"][:40])
        if h in seen:
            reason = f"FU3 duplicate of #{seen[h]['num']} — same action from multiple transcripts"
            to_close[r["num"]] = (r, reason)
        else:
            seen[h] = r

    # ── FU1: staleness ───────────────────────────────────────────────────────
    for r in rows:
        if r["num"] in to_close:
            continue
        dd = r["dd"]
        if not dd:
            continue
        days = (today - dd).days
        if days < _FU1_DAYS:
            continue
        if _ONE_TIME_RE.match(r["what"].strip()):
            reason = f"FU1 stale: one-time action {days}d past due — window expired"
            to_close[r["num"]] = (r, reason)

    # ── FU2: deal stage promotion ────────────────────────────────────────────
    for r in rows:
        if r["num"] in to_close:
            continue
        dd = r["dd"]
        if not dd:
            continue
        days = (today - dd).days
        if days < _FU2_MIN_DAYS_PAST:
            continue
        slug = _deal_slug_for_row(r["who"], r["what"])
        if not slug:
            continue
        count = log_counts.get(slug, 0)
        if count < _FU2_MIN_LOG_ENTRIES:
            continue
        if _EARLY_STAGE_RE.search(r["what"]):
            reason = (
                f"FU2 superseded: early-stage action for {slug} "
                f"({count} log entries — deal actively engaged)"
            )
            to_close[r["num"]] = (r, reason)

    if verbose:
        print(f"  FU3 dupes:      {sum(1 for _,(_,r) in to_close.items() if 'FU3' in r)}")
        print(f"  FU1 stale:      {sum(1 for _,(_,r) in to_close.items() if 'FU1' in r)}")
        print(f"  FU2 superseded: {sum(1 for _,(_,r) in to_close.items() if 'FU2' in r)}")

    # Build (old_snippet, new_snippet, reason) triples
    results = []
    for num, (r, reason) in to_close.items():
        old_snippet = r["raw"][:100]
        what_start = r["what"][:20]
        tag = f"[RESOLVED {today_str} — {reason[:70]}]"
        new_snippet = old_snippet.replace(f"| {what_start}", f"| {tag} {what_start}", 1)
        if old_snippet == new_snippet:
            # Fallback: replace first occurrence of what_start in the line
            new_snippet = old_snippet.replace(what_start[:15], f"{tag} {what_start[:15]}", 1)
        if old_snippet != new_snippet:
            results.append((old_snippet, new_snippet, reason, num))
        elif verbose:
            print(f"    SKIP #{num}: snippet replace failed", file=sys.stderr)

    return results


def run_sweep(dry_run: bool = False, verbose: bool = False) -> dict:
    """Run all three sweeps. Returns stats dict."""
    today = datetime.now(timezone.utc).date()
    stats = {"fu1": 0, "fu2": 0, "fu3": 0, "total": 0, "errors": 0}

    try:
        docs_svc = _get_docs_service()
    except Exception as e:
        print(f"followup_sweep: Google Docs unavailable — {e}", file=sys.stderr)
        return stats

    try:
        rows = _fetch_open_rows(docs_svc)
    except Exception as e:
        print(f"followup_sweep: doc fetch failed — {e}", file=sys.stderr)
        return stats

    log_counts = _load_deal_log_counts()
    requests_data = _build_requests(rows, log_counts, today, verbose)

    if not requests_data:
        if verbose:
            print("followup_sweep: nothing to close today")
        return stats

    if dry_run:
        print(f"followup_sweep [dry-run]: would close {len(requests_data)} rows")
        for old, new, reason, num in requests_data[:20]:
            print(f"  #{num:4}  {reason[:80]}")
        return stats

    # Execute in batches of 50 (Docs API limit)
    requests = [
        {"replaceAllText": {
            "containsText": {"text": old, "matchCase": True},
            "replaceText": new,
        }}
        for old, new, reason, num in requests_data
    ]

    total_ok = 0
    for i in range(0, len(requests), 50):
        batch = requests[i : i + 50]
        try:
            resp = docs_svc.documents().batchUpdate(
                documentId=FOLLOWUPS_DOC_ID,
                body={"requests": batch},
            ).execute()
            replies = resp.get("replies", [])
            ok = sum(1 for r in replies if r.get("replaceAllText", {}).get("occurrencesChanged", 0) > 0)
            total_ok += ok
        except Exception as e:
            print(f"followup_sweep: batchUpdate error — {e}", file=sys.stderr)
            stats["errors"] += len(batch)

    stats["total"] = total_ok
    for _, _, reason, _ in requests_data:
        if "FU1" in reason:
            stats["fu1"] += 1
        elif "FU2" in reason:
            stats["fu2"] += 1
        elif "FU3" in reason:
            stats["fu3"] += 1

    print(
        f"followup_sweep: closed {total_ok} rows "
        f"(FU1={stats['fu1']} FU2={stats['fu2']} FU3={stats['fu3']})",
        file=sys.stderr,
    )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="Report without writing")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    try:
        stats = run_sweep(dry_run=args.dry_run, verbose=args.verbose)
        if args.verbose:
            print(json.dumps(stats, indent=2))
        return 0
    except Exception as e:
        print(f"followup_sweep FATAL: {e}", file=sys.stderr)
        return 0  # always exit 0 — sweep is non-fatal


if __name__ == "__main__":
    sys.exit(main())
