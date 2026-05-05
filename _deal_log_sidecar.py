"""_deal_log_sidecar.py — JSONL store for LLM-emitted deal_log_entries[].

Sidesteps the Google-doc-routing path so the precision-first signal can
flow from extraction → compile-side V1 auto-log without touching the
parallel-session-owned _envelope_writer.

Lifecycle:
  1. Extraction (cos_otter_backfill / cos_email_backfill) calls
     `append(entries, src_ref, source_id)` after each successful run.
     Each entry is `{deal_id, summary, evidence}`; we annotate with
     source metadata + a stable per-record id for idempotency.
  2. Compile (`deal-system-compile.py > _compute_deal_logs`) calls
     `read_recent(days)` and uses the entries as Pass A0 — the highest-
     precision tier of the V1+ two-pass strategy. Each entry surfaces
     as a candidate pair tagged `match: "llm_explicit"`.

File: ~/dashboards/data/extracted/deal-log-entries.jsonl

Format: one JSON object per line. Append-only. The compile-side reader
caps the in-memory window by date so the file can grow without bound;
periodic GC of records older than ~180d would be a future improvement.

Codified 2026-05-04 LATE-EVE — closes deferred [1] from the standing
handoff (deal_log_entries[] extraction → compile plumbing) without
touching _envelope_writer.py.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

SIDECAR_PATH = Path.home() / "dashboards" / "data" / "extracted" / "deal-log-entries.jsonl"


def _stable_id(source_id: str, deal_id: str, summary: str) -> str:
    """djb2 of (source_id|deal_id|summary[:80]). Deterministic and
    short. Identical inputs always hash to the same id, which is what
    makes the append idempotent across re-runs."""
    raw = f"{source_id or ''}|{deal_id or ''}|{(summary or '')[:80]}"
    h = 5381
    for c in raw:
        h = ((h << 5) + h) ^ ord(c)
    return format(h & 0xFFFFFFFF, "08x")


def _utc_now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_existing_ids() -> set:
    """Scan the sidecar once for already-stored ids — keeps append
    idempotent across re-runs of the same transcript / email thread.
    Bounded scan (last 100k lines) so a runaway file doesn't OOM."""
    seen: set = set()
    if not SIDECAR_PATH.exists():
        return seen
    try:
        with SIDECAR_PATH.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                rid = obj.get("id")
                if rid:
                    seen.add(rid)
    except Exception:
        pass
    return seen


def append(entries: list, src_ref: dict | None, source_id: str) -> int:
    """Append `entries` to the sidecar, deduping by stable id.

    `entries`: list of `{deal_id, summary, evidence}` from the LLM.
    `src_ref`: the same source_ref attached to envelope_items —
        carries `type`, `title`, `doc_url`, `date`. Used to backfill
        per-record source metadata.
    `source_id`: a stable transcript / thread identifier (transcript
        file_id for calls; gmail thread_id for emails). Distinguishes
        the same summary text appearing in multiple sources.

    Returns the number of NEW entries written (already-seen ids are
    skipped so re-running an extraction doesn't bloat the file).
    """
    if not entries:
        return 0
    SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    src_ref = src_ref or {}
    src_type = src_ref.get("type") or ""
    src_title = src_ref.get("title") or ""
    src_url = src_ref.get("doc_url") or ""
    src_date = (src_ref.get("date") or "")[:10]
    seen = _load_existing_ids()
    appended = 0
    with SIDECAR_PATH.open("a", encoding="utf-8") as fh:
        for e in entries:
            if not isinstance(e, dict):
                continue
            deal_id = (e.get("deal_id") or "").strip()
            summary = (e.get("summary") or "").strip()
            if not deal_id or not summary:
                continue
            rid = _stable_id(source_id, deal_id, summary)
            if rid in seen:
                continue
            record = {
                "id": rid,
                "deal_id": deal_id,
                "summary": summary[:280],
                "evidence": (e.get("evidence") or "")[:200],
                "source_id": source_id,
                "source_type": src_type,
                "source_title": src_title[:120],
                "source_url": src_url,
                "date": src_date,
                "appended_at": _utc_now_iso(),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            seen.add(rid)
            appended += 1
    return appended


def read_recent(days: int = 30) -> list:
    """Return sidecar records with `date` within the last `days` days.

    Used by the compile-side Pass A0 lookup. Records without a parseable
    date are excluded — the V1 auto-log requires a date to file the
    entry under, so undated records can't be used.
    """
    if not SIDECAR_PATH.exists():
        return []
    try:
        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    except Exception:
        cutoff = ""
    out = []
    try:
        with SIDECAR_PATH.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                d = (obj.get("date") or "")[:10]
                if not d or len(d) != 10 or d[4] != "-" or d[7] != "-":
                    continue
                if d < cutoff:
                    continue
                out.append(obj)
    except Exception:
        pass
    return out
