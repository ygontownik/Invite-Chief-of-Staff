#!/usr/bin/env python3
"""heartbeat_email.py — Phase 2 Track L2 consolidated heartbeat email.

Reads ``~/cos-pipeline/data-<tenant>/heartbeat.json`` for every tenant tree
under ``~/cos-pipeline/data-*/``, builds a single consolidated email body
(one section per tenant, stale routines first), and either prints it
(``--dry-run`` — DEFAULT ON) or attempts to hand off to the existing
``send_briefing_email`` helper.

CLI:
  python3 heartbeat_email.py --to addr@example.com [--threshold 24] [--dry-run]

Per overnight rules: ``--dry-run`` is the default tonight. We never actually
send an email until the user flips the switch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


HOME = Path(os.path.expanduser("~"))
DATA_GLOB = "data-*"
HEARTBEAT_FILENAME = "heartbeat.json"
DEFAULT_THRESHOLD = 24.0


def _load_tenants() -> list[tuple[str, dict[str, Any]]]:
    """Return [(tenant_slug, payload_dict), ...] for every heartbeat.json found."""
    base = HOME / "cos-pipeline"
    out: list[tuple[str, dict[str, Any]]] = []
    if not base.exists():
        return out
    for d in sorted(base.glob(DATA_GLOB)):
        if not d.is_dir():
            continue
        tenant = d.name[len("data-") :] if d.name.startswith("data-") else d.name
        hb = d / HEARTBEAT_FILENAME
        if not hb.exists():
            out.append((tenant, {"error": f"no {HEARTBEAT_FILENAME}"}))
            continue
        try:
            payload = json.loads(hb.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        out.append((tenant, payload))
    return out


def _format_tenant_section(
    tenant: str, payload: dict[str, Any], threshold: float
) -> list[str]:
    lines: list[str] = []
    lines.append(f"=== Tenant: {tenant} ===")
    if "error" in payload:
        lines.append(f"  ERROR: {payload['error']}")
        lines.append("")
        return lines
    lines.append(f"  Generated: {payload.get('generated_at', 'unknown')}")
    routines = payload.get("routines", [])
    by_status: dict[str, list[dict[str, Any]]] = {}
    for r in routines:
        by_status.setdefault(r.get("status", "unknown"), []).append(r)
    counts = {k: len(v) for k, v in by_status.items()}
    summary = " | ".join(
        f"{k}={counts.get(k, 0)}" for k in ("ok", "stale", "probe-error", "excluded")
    )
    lines.append(f"  Counts: {summary}")
    lines.append("")
    stale = sorted(
        by_status.get("stale", []),
        key=lambda r: (r.get("staleness_hours") or 0),
        reverse=True,
    )
    if stale:
        lines.append("  STALE routines (oldest first):")
        for r in stale:
            stale_h = r.get("staleness_hours")
            stale_str = "no log" if stale_h is None else f"{stale_h:.1f}h"
            note = "; ".join(r.get("notes", [])) if r.get("notes") else ""
            lines.append(
                f"    - {r.get('name')}  [{r.get('kind')}/"
                f"{r.get('package') or '?'}]  {stale_str}"
                + (f"  ({note})" if note else "")
            )
        lines.append("")
    err = by_status.get("probe-error", [])
    if err:
        lines.append("  PROBE ERRORS:")
        for r in err:
            note = "; ".join(r.get("notes", [])) if r.get("notes") else ""
            lines.append(f"    - {r.get('name')}: {note}")
        lines.append("")
    return lines


def build_body(tenants: list[tuple[str, dict[str, Any]]], threshold: float) -> str:
    lines: list[str] = []
    lines.append(
        f"Heartbeat report — {dt.datetime.now().isoformat(timespec='seconds')}"
    )
    lines.append(f"Threshold: {threshold}h")
    lines.append("")
    if not tenants:
        lines.append("No tenants found (no ~/cos-pipeline/data-*/heartbeat.json).")
        return "\n".join(lines) + "\n"
    total_stale = 0
    total_err = 0
    for _t, payload in tenants:
        for r in payload.get("routines", []) or []:
            if r.get("status") == "stale":
                total_stale += 1
            elif r.get("status") == "probe-error":
                total_err += 1
    lines.append(
        f"Across {len(tenants)} tenant(s): {total_stale} stale, "
        f"{total_err} probe-error."
    )
    lines.append("")
    for tenant, payload in tenants:
        lines.extend(_format_tenant_section(tenant, payload, threshold))
    return "\n".join(lines) + "\n"


def _try_import_sender() -> Optional[Any]:
    """Best-effort import of the existing send_briefing_email helper.

    The actual function lives at ``~/dashboards/routines/brief/send_briefing_email.py``.
    We do NOT modify it. We import it; if anything fails, we fall back to print.
    """
    candidates = [
        HOME / "dashboards" / "routines" / "brief",
        HOME / "dashboards" / "routines",
    ]
    for c in candidates:
        if c.exists() and str(c) not in sys.path:
            sys.path.insert(0, str(c))
    try:
        return importlib.import_module("send_briefing_email")
    except Exception:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True, help="Recipient email address.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print body only; never send. DEFAULT ON tonight.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Override default: actually attempt to send (still requires sender import).",
    )
    args = parser.parse_args(argv)

    tenants = _load_tenants()
    body = build_body(tenants, args.threshold)
    subject = f"[Heartbeat] {sum(1 for _, p in tenants for r in p.get('routines', []) or [] if r.get('status') == 'stale')} stale routine(s)"

    if args.dry_run:
        print(f"--- DRY RUN — would send to {args.to} ---")
        print(f"Subject: {subject}")
        print()
        print(body)
        return 0

    sender = _try_import_sender()
    if sender is None:
        print(
            "[heartbeat_email] send_briefing_email not importable; falling back to print.",
            file=sys.stderr,
        )
        print(f"To: {args.to}")
        print(f"Subject: {subject}")
        print()
        print(body)
        return 0

    # Try a few common entry-point names without modifying the module.
    for fname in ("send_briefing_email", "send_email", "send"):
        fn = getattr(sender, fname, None)
        if callable(fn):
            try:
                fn(to=args.to, subject=subject, body=body)
                print(f"[heartbeat_email] sent via {fname}() to {args.to}")
                return 0
            except TypeError:
                # Maybe positional signature
                try:
                    fn(args.to, subject, body)
                    print(f"[heartbeat_email] sent via {fname}() to {args.to}")
                    return 0
                except Exception as exc:
                    print(
                        f"[heartbeat_email] {fname} call failed: {exc}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[heartbeat_email] {fname} call failed: {exc}", file=sys.stderr
                )
    print(
        "[heartbeat_email] no callable entry-point found in send_briefing_email; "
        "printing instead.",
        file=sys.stderr,
    )
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
