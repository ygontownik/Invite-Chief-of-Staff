#!/usr/bin/env python3
"""cos_artifact_ingest.py — Ingest claude.ai project artifacts → structured pipeline output.

Phase J of the synthesis architecture. Watches ~/Downloads/_Routed/<slug>/*.md
for analytical artifacts produced by claude.ai project chats, extracts:

  1. DEAL-INTEL blocks  → routed via intel_capture.py (existing path)
  2. Proposed follow-ups → data/staging/proposed-followups.jsonl
  3. Entity mentions     → data/deals/<slug>/entity_mentions.json
                          (consumed by Phase I gap detector)

100% Claude Max via _claude_dispatch — no raw Anthropic SDK surface.

Usage:
  python3 cos_artifact_ingest.py                 # all deals, incremental
  python3 cos_artifact_ingest.py --deal <slug>   # single deal
  python3 cos_artifact_ingest.py --dry-run       # no writes, print extraction
  python3 cos_artifact_ingest.py --force         # re-ingest already-processed

Design: ~/dashboards/docs/DESIGN-phase-J-artifact-ingest.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import _firm_context as _fc  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────
_ROUTED_ROOT = Path.home() / "Downloads" / "_Routed"
_DEALS_DIR = Path.home() / "dashboards" / "data" / "deals"
_STAGING_DIR = Path.home() / "dashboards" / "data" / "staging"
_PROPOSED_FOLLOWUPS = _STAGING_DIR / "proposed-followups.jsonl"
_REJECTED_FOLLOWUPS = _STAGING_DIR / "rejected-followups.jsonl"
_PROMPT_PATH = Path.home() / "dashboards" / "config" / "artifact-ingest-prompt.md"
_INTEL_CAPTURE_BIN = Path.home() / "cos-pipeline" / "tools" / "intel_capture.py"

_MAX_ARTIFACT_CHARS = 80_000
_MAX_TOKENS = 8192
_MODEL = "claude-sonnet-4-6"
_DEFAULT_AUTOAPPLY_THRESHOLD = 0.95  # first week conservative; lower after telemetry
_REJECT_THRESHOLD = 0.40

_DRIVE_DOC_FOLDER_URL = "https://drive.google.com/drive/folders/{fid}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _load_drive_yaml() -> dict:
    """Return the raw parsed drive-docs.yaml (deal_docs section needed)."""
    config_dir = _fc._find_config_dir()
    path = config_dir / "drive-docs.yaml"
    if not path.exists():
        return {}
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def _list_registered_deals(drive_yaml: dict) -> list[dict]:
    """Yield {'slug', 'name', 'sector', 'drive_folder_id'} for each registered deal."""
    out = []
    for slug, entry in (drive_yaml.get("deal_docs") or {}).items():
        if not isinstance(entry, dict):
            continue
        out.append({
            "slug": slug,
            "name": entry.get("name", slug),
            "sector": entry.get("sector", ""),
            "drive_folder_id": entry.get("drive_folder_id", ""),
        })
    return out


def _sha256_8(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _load_artifacts_tracker(deal_slug: str) -> dict:
    p = _DEALS_DIR / deal_slug / "artifacts.json"
    if not p.exists():
        return {"deal_id": deal_slug, "artifacts": {}}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"deal_id": deal_slug, "artifacts": {}}


def _save_artifacts_tracker(deal_slug: str, tracker: dict) -> None:
    p = _DEALS_DIR / deal_slug / "artifacts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))


def _load_entity_mentions(deal_slug: str) -> dict:
    p = _DEALS_DIR / deal_slug / "entity_mentions.json"
    if not p.exists():
        return {"deal_id": deal_slug, "entities": {}}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"deal_id": deal_slug, "entities": {}}


def _save_entity_mentions(deal_slug: str, data: dict) -> None:
    p = _DEALS_DIR / deal_slug / "entity_mentions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _list_artifacts(deal_slug: str) -> list[Path]:
    """Return .md (and future: .jsx) files in _Routed/<slug>/, oldest first."""
    src = _ROUTED_ROOT / deal_slug
    if not src.is_dir():
        return []
    files = sorted(src.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return files


def _read_artifact(path: Path) -> str:
    text = path.read_text(errors="replace")
    if len(text) > _MAX_ARTIFACT_CHARS:
        return text[:_MAX_ARTIFACT_CHARS] + "\n\n[artifact truncated]"
    return text


def _render_prompt(template: str, ctx: dict, deal: dict, artifact_path: Path,
                   artifact_title: str) -> str:
    principal = _fc._principal(ctx) or {}
    firm = _fc._firm(ctx) or {}
    subs = {
        "{firm_display}": firm.get("name", "the firm"),
        "{firm_short}": firm.get("short_name", firm.get("name", "")),
        "{principal_first}": (principal.get("name") or "the principal").split()[0],
        "{deal_slug}": deal["slug"],
        "{deal_name}": deal["name"],
        "{deal_sector}": deal["sector"],
        "{artifact_path}": str(artifact_path),
        "{artifact_title}": artifact_title,
        "{workstream_deal}": _fc.workstream_deal(ctx),
        "{workstream_recruiting}": _fc.workstream_recruiting(ctx),
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


def _extract_h1(text: str, fallback: str) -> str:
    for line in text.splitlines()[:10]:
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    # Strip control chars that break json.loads
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", t)
    return t.strip()


def _truncation_close(text: str) -> dict | None:
    """Close unclosed strings/brackets/braces caused by LLM output truncation."""
    stack: list[str] = []
    in_str = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_str:
            escape_next = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    tail = ('"' if in_str else "") + "".join(reversed(stack))
    repaired = re.sub(r",\s*$", "", text.rstrip()) + tail
    repaired = re.sub(r",\s*(?=[\}\]])", "", repaired)
    try:
        return json.loads(repaired, strict=False)
    except json.JSONDecodeError:
        return None


def _repair_json(text: str) -> dict | None:
    """Multi-stage local JSON repair for malformed LLM output. Returns dict or None."""
    # Stage 1: trailing commas before } or ]
    c1 = re.sub(r",\s*(?=[\}\]])", "", text)
    try:
        return json.loads(c1, strict=False)
    except json.JSONDecodeError:
        pass

    # Stage 2: Python literal tokens → JSON equivalents
    c2 = c1.replace(": None", ": null").replace(": True", ": true").replace(": False", ": false")
    try:
        return json.loads(c2, strict=False)
    except json.JSONDecodeError:
        pass

    # Stage 3: extract outermost { … } block (strips stray prose)
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e > s:
        snippet = re.sub(r",\s*(?=[\}\]])", "", text[s : e + 1])
        try:
            return json.loads(snippet, strict=False)
        except json.JSONDecodeError:
            pass

    # Stage 4: truncation repair — close all unclosed nesting
    return _truncation_close(text)


def _call_claude(system_prompt: str, artifact_text: str) -> dict:
    """Single Sonnet call via _claude_dispatch. Returns parsed JSON.

    Falls back through four local repair stages then a single LLM retry before
    raising JSONDecodeError. Proven failure mode: 40KB+ artifacts where Sonnet
    emits unescaped quotes or trailing commas inside multiline DEAL-INTEL strings.
    """
    import _claude_dispatch  # noqa: PLC0415

    raw = _claude_dispatch.call(
        task_type="cos_artifact_ingest",
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": artifact_text}],
        api_timeout=180,
    )
    cleaned = _strip_json_fences(raw)

    # Fast path — clean output (most artifacts)
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        pass

    # Local repair stages — no extra LLM cost
    repaired = _repair_json(cleaned)
    if repaired is not None:
        print("  [artifact_ingest] JSON repaired locally (trailing commas / truncation)",
              file=sys.stderr)
        return repaired

    # LLM retry — pass malformed response back, ask for clean re-emit
    print("  [artifact_ingest] JSON parse failed — retrying with explicit JSON prompt",
          file=sys.stderr)
    raw2 = _claude_dispatch.call(
        task_type="cos_artifact_ingest",
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=[
            {"role": "user", "content": artifact_text},
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. "
                    "Reemit ONLY the JSON object — no prose, no code fences. "
                    "Escape any literal double-quote characters inside string values as \\\"."
                ),
            },
        ],
        api_timeout=180,
    )
    cleaned2 = _strip_json_fences(raw2)
    try:
        return json.loads(cleaned2, strict=False)
    except json.JSONDecodeError:
        pass
    repaired2 = _repair_json(cleaned2)
    if repaired2 is not None:
        print("  [artifact_ingest] JSON repaired after LLM retry", file=sys.stderr)
        return repaired2

    raise json.JSONDecodeError("Extraction failed after repair + retry", cleaned, 0)


def _route_intel_blocks(blocks: list[str]) -> tuple[int, int]:
    """Pipe DEAL-INTEL blocks to intel_capture.py parse-stdin. Returns (routed, errors)."""
    if not blocks:
        return 0, 0
    payload = "\n\n".join(blocks)
    try:
        result = subprocess.run(
            [sys.executable, str(_INTEL_CAPTURE_BIN), "parse-stdin"],
            input=payload, capture_output=True, text=True, timeout=60,
        )
        # parse-stdin's last line: "parse-stdin: routed=N, skipped=N, errors=N"
        m = re.search(r"routed=(\d+).*errors=(\d+)", result.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
        if result.returncode != 0:
            print(f"  [intel_capture stderr] {result.stderr.strip()[:200]}", file=sys.stderr)
            return 0, len(blocks)
        return len(blocks), 0
    except subprocess.TimeoutExpired:
        return 0, len(blocks)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _merge_entity_mentions(existing: dict, new: list[dict], artifact_id: str,
                           artifact_path: str) -> dict:
    """Merge new entity mentions into existing catalog. Keyed by canonical name."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ent in new:
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        record = existing["entities"].setdefault(name, {
            "name": name,
            "role": ent.get("role", "other"),
            "first_seen": today,
            "last_seen": today,
            "total_mentions": 0,
            "sources": [],
        })
        record["last_seen"] = today
        record["total_mentions"] += int(ent.get("mention_count", 1))
        # Append source if not already present
        src_entry = {
            "artifact_id": artifact_id,
            "artifact_path": artifact_path,
            "mentions_in_artifact": int(ent.get("mention_count", 1)),
            "summary": ent.get("summary", "")[:200],
            "date": today,
        }
        if not any(s.get("artifact_id") == artifact_id for s in record["sources"]):
            record["sources"].append(src_entry)
        # Promote role if existing is "other" and new is more specific
        if record["role"] == "other" and ent.get("role") and ent["role"] != "other":
            record["role"] = ent["role"]
    return existing


# ── Main ingestion loop ───────────────────────────────────────────────────────
def ingest_artifact(deal: dict, artifact: Path, ctx: dict, prompt_template: str,
                    dry_run: bool = False) -> dict:
    """Process one artifact. Returns stats dict."""
    raw_text = _read_artifact(artifact)
    artifact_title = _extract_h1(raw_text, artifact.stem)
    prompt = _render_prompt(prompt_template, ctx, deal, artifact, artifact_title)

    print(f"  → extracting {artifact.name} ({len(raw_text):,} chars) ...", flush=True)
    try:
        result = _call_claude(prompt, raw_text)
    except Exception as e:
        print(f"    ✗ extraction failed: {e!r}", file=sys.stderr)
        return {"status": "error", "error": str(e)}

    intel_blocks = result.get("deal_intel") or []
    followups = result.get("proposed_followups") or []
    entities = result.get("entity_mentions") or []

    print(f"    intel={len(intel_blocks)} followups={len(followups)} entities={len(entities)}",
          flush=True)

    if dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])
        return {"status": "dry_run", "intel": len(intel_blocks),
                "followups": len(followups), "entities": len(entities)}

    # 1. Route intel blocks via intel_capture.py
    routed, intel_errors = _route_intel_blocks(intel_blocks)

    # 2. Annotate + write follow-ups (stage all; applier decides auto-apply)
    artifact_id = f"{deal['slug']}-{_sha256_8(artifact)}"
    drive_url = (_DRIVE_DOC_FOLDER_URL.format(fid=deal["drive_folder_id"])
                 if deal["drive_folder_id"] else "")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    staged, rejected = [], []
    for f in followups:
        f.setdefault("source", f"claude.ai — {artifact_title}")
        f.setdefault("linked_to", drive_url)
        f["deal_slug"] = deal["slug"]
        f["artifact_id"] = artifact_id
        f["artifact_path"] = str(artifact)
        f["ingested_at"] = today_iso
        conf = float(f.get("confidence", 0.0) or 0.0)
        if conf < _REJECT_THRESHOLD:
            rejected.append(f)
        else:
            staged.append(f)
    if staged:
        _append_jsonl(_PROPOSED_FOLLOWUPS, staged)
    if rejected:
        _append_jsonl(_REJECTED_FOLLOWUPS, rejected)

    # 3. Merge entity mentions
    em = _load_entity_mentions(deal["slug"])
    em = _merge_entity_mentions(em, entities, artifact_id, str(artifact))
    em["updated_at"] = today_iso + "T" + datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    _save_entity_mentions(deal["slug"], em)

    return {
        "status": "ok",
        "artifact_id": artifact_id,
        "artifact_title": artifact_title,
        "intel_routed": routed,
        "intel_errors": intel_errors,
        "followups_staged": len(staged),
        "followups_rejected": len(rejected),
        "entities_merged": len(entities),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest claude.ai project artifacts.")
    ap.add_argument("--deal", help="Restrict to a single deal slug")
    ap.add_argument("--dry-run", action="store_true",
                    help="No writes; print parsed JSON only")
    ap.add_argument("--force", action="store_true",
                    help="Re-ingest already-processed artifacts")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap artifacts processed per run (0=unbounded)")
    args = ap.parse_args()

    print(f"=== cos_artifact_ingest ({datetime.now(timezone.utc).isoformat(timespec='seconds')}) ===")

    # Load firm context + drive yaml + prompt template
    ctx = _fc.load_firm_context() or {}
    drive_yaml = _load_drive_yaml()
    if not _PROMPT_PATH.exists():
        print(f"ERROR: prompt template missing: {_PROMPT_PATH}", file=sys.stderr)
        return 2
    prompt_template = _PROMPT_PATH.read_text()

    deals = _list_registered_deals(drive_yaml)
    if args.deal:
        deals = [d for d in deals if d["slug"] == args.deal]
        if not deals:
            print(f"ERROR: deal '{args.deal}' not in registry", file=sys.stderr)
            return 2

    total_processed = total_skipped = total_errors = 0
    total_intel = total_followups = total_entities = 0
    for deal in deals:
        artifacts = _list_artifacts(deal["slug"])
        if not artifacts:
            continue
        tracker = _load_artifacts_tracker(deal["slug"])
        print(f"\n[{deal['slug']}] {len(artifacts)} artifact(s) in _Routed")

        for art in artifacts:
            sha = _sha256_8(art)
            artifact_key = f"{art.name}:{sha}"
            already = artifact_key in tracker["artifacts"]
            if already and not args.force:
                total_skipped += 1
                continue
            if args.limit and total_processed >= args.limit:
                print(f"  [limit reached: {args.limit}]")
                break

            stats = ingest_artifact(deal, art, ctx, prompt_template, dry_run=args.dry_run)
            if stats["status"] == "ok":
                total_processed += 1
                total_intel += stats["intel_routed"]
                total_followups += stats["followups_staged"]
                total_entities += stats["entities_merged"]
                tracker["artifacts"][artifact_key] = {
                    "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "title": stats["artifact_title"],
                    "intel_routed": stats["intel_routed"],
                    "followups_staged": stats["followups_staged"],
                    "followups_rejected": stats["followups_rejected"],
                    "entities_merged": stats["entities_merged"],
                }
                if not args.dry_run:
                    _save_artifacts_tracker(deal["slug"], tracker)
            elif stats["status"] == "dry_run":
                total_processed += 1
            else:
                total_errors += 1

    print(f"\n=== summary ===")
    print(f"  artifacts processed : {total_processed}")
    print(f"  artifacts skipped   : {total_skipped} (already in tracker)")
    print(f"  artifacts errored   : {total_errors}")
    print(f"  intel blocks routed : {total_intel}")
    print(f"  followups staged    : {total_followups}")
    print(f"  entities merged     : {total_entities}")

    # Dashboard warmup (non-blocking) so newly-merged data lands in the next render
    if total_processed and not args.dry_run:
        try:
            req = urllib.request.Request("http://localhost:7777/warmup",
                                          method="POST", data=b"")
            urllib.request.urlopen(req, timeout=5)
            print("  dashboard warmup    : triggered")
        except Exception:
            pass

    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
