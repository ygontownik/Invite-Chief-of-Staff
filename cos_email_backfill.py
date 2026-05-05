#!/usr/bin/env python3
"""
cos_email_backfill.py — Gmail poller for the dashboard-capture label.

Mirrors the architecture of cos_otter_backfill.py:
  1. Refresh OAuth token (Gmail-only pickle at ~/credentials/gmail_token.pickle).
  2. Enumerate threads tagged `dashboard-capture` (+ sub-labels for parent
     hints like `dashboard-capture/<deal-slug>`).
  3. Dedup by thread-id + message history-id in
     ~/credentials/processed_emails.json. A re-run where the latest
     message hasn't changed is a no-op.
  4. Extract sender, recipients, subject, full body (plain-text preferred),
     attachment metadata. Never downloads attachments — records filename /
     mimeType / attachment_id on source_ref for later gated approval.
  5. Runs Sonnet with the SAME four-block cached preamble structure as
     the Otter backfill — routing rules, stable preamble, pipeline
     context, per-thread dynamic block.
  6. Routes envelope_items via _envelope_writer.append_items().
  7. POSTs to http://localhost:7777/warmup.
  8. Logs a run-summary line matching the Otter pipeline shape.

Scopes: gmail.readonly + gmail.labels ONLY. This pipeline never sends,
composes, modifies, or forwards. See docs/CLAUDE.md security rules and
scripts/bootstrap-gmail-auth.sh for the consent flow.

── TENANT CONFIG ─────────────────────────────────────────────────────
This script is tenant-agnostic: every firm/principal-specific value is
loaded at runtime from `firm_context.yaml` in the tenant config dir
(default `~/cos-pipeline-config-<slug>/`, override via `$COS_CONFIG_DIR`
or `--config-dir`). Keys consumed:

  principal.name / .email / .role / .background — owner attribution,
                                                   prompt header
  firm.name      / .short_name                  — DEAL PIPELINE TARGETS
                                                   header label
  team[]                                        — speaker / role context
  owner_whitelist                               — JSON owner enum
  workstream_categories.deal                    — workstream tag
  counterparty_aliases / peer_firms / key_people — cross-reference hints
                                                   surfaced via the
                                                   shared header builder

── ENV VARS ──────────────────────────────────────────────────────────
  ANTHROPIC_API_KEY     — required when auth_mode=api (subscription mode
                          uses CLAUDE_CODE_OAUTH_TOKEN via _claude_dispatch).
  COS_CONFIG_DIR        — explicit tenant config dir override.

── INVOKING AS A NEW SUBSCRIBER ──────────────────────────────────────
  1. Clone/scaffold your tenant config to ~/cos-pipeline-config-<slug>/
     (see firm_context.template.yaml for the schema).
  2. Bootstrap Gmail auth: ~/dashboards/scripts/bootstrap-gmail-auth.sh
  3. Run: python3 cos_email_backfill.py --backfill --list
     (or `--config-dir ~/cos-pipeline-config-<slug>` to be explicit).

CLI:
  --list                Enumerate matching threads; do not process.
  --backfill            Force the first-run window even if dedup has entries.
  --force               Re-process threads already in the dedup tracker.
  --id THREAD_ID        Limit to one Gmail thread id (implies --force for it).
  --since YYYY-MM-DD    Override the Gmail q= date filter.
  --config-dir PATH     Override tenant config directory (sets COS_CONFIG_DIR).
  --print-prompt        Diagnostic: print the rendered LLM preamble and exit.
  --download-approved MESSAGE_ID
                        Helper — prints attachments Gmail would download
                        and writes them to Drive. Gated explicitly per
                        ~/.claude/CLAUDE.md safety rules.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# Path setup — the file may live at either:
#   ~/dashboards/routines/process/cos_email_backfill.py  (legacy)
#   ~/cos-pipeline/cos_email_backfill.py                  (canonical, post-U4 refactor)
# Either way, _ROOT points at the dashboards repo (data + config) and
# the cos-pipeline directory is added to sys.path so we can import the
# shared helpers (_usage, _entity_normalizer, _firm_context).
_HERE        = Path(__file__).resolve().parent
_COS_DIR     = Path.home() / "cos-pipeline"
_ROOT        = Path.home() / "dashboards"
for p in (str(_COS_DIR), str(_HERE.parent), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
try:
    from _usage import log_usage  # noqa: E402
except Exception:  # pragma: no cover
    def log_usage(*_a, **_kw):
        return

import _firm_context as _fc  # noqa: E402

# ── Early --config-dir handling ───────────────────────────────────────────────
# Honor `--config-dir PATH` before _CTX loads at module scope so the override
# applies to every downstream load (firm_context.yaml, drive-docs.yaml, etc.).
# Falls through silently when the flag is absent — argparse handles validation
# in main(). Env var COS_CONFIG_DIR is the canonical knob _firm_context reads.
def _peek_config_dir() -> None:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--config-dir" and i + 1 < len(argv):
            os.environ["COS_CONFIG_DIR"] = str(Path(argv[i + 1]).expanduser())
            return
        if a.startswith("--config-dir="):
            os.environ["COS_CONFIG_DIR"] = str(Path(a.split("=", 1)[1]).expanduser())
            return

_peek_config_dir()

_CTX             = _fc.load_firm_context()
_PRINCIPAL_FULL  = _fc.principal_full_name(_CTX)
_PRINCIPAL_FIRST = _fc.principal_first_name(_CTX)
_PRINCIPAL_EMAIL = _fc.principal_email(_CTX)
_PRINCIPAL_ROLE  = _fc.principal_role(_CTX)
_DEAL_LEAD_NAME  = _fc.deal_lead_name(_CTX)
_OWNERS_PIPE     = _fc.owner_whitelist_str(_CTX)   # e.g. "Principal|Partner|Analyst"
_OWNER_LIST      = _CTX.get("owner_whitelist", []) or []
_OWNERS_QUOTED   = ", ".join(f'"{o}"' for o in _OWNER_LIST + ["external"])
_DEAL_WS         = _fc.workstream_deal(_CTX)
_FIRM_NAME       = _fc.firm_name(_CTX)
_FIRM_SHORT      = (_CTX.get("firm", {}) or {}).get("short_name", "") or _FIRM_NAME
# Header label for the cross-reference block emitted to the LLM. Uses the
# deal-workstream tag (firm_context.yaml :: workstream_categories.deal),
# which is the same colloquial form already used elsewhere in the pipeline
# (example: "<WORKSTREAM_DEAL> DEAL PIPELINE TARGETS" — substituted at
# runtime from tenant config). Preserves the pre-parameterization label.
_PIPELINE_BLOCK_LABEL = f"{(_DEAL_WS or _FIRM_SHORT or 'FIRM').upper()} DEAL PIPELINE TARGETS"

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_TOKEN_PATH     = Path.home() / "credentials/gmail_token.pickle"
CLIENT_SECRET_PATH   = Path.home() / "credentials/client_secret.json"
DEDUP_PATH           = Path.home() / "credentials/processed_emails.json"
PIPELINE_DATA_PATH   = _ROOT / "data/compiled/deal-pipeline-data.json"
DASHBOARD_DATA_PATH  = _ROOT / "data/compiled/dashboard-data.json"
ROUTING_RULES_PATH   = _ROOT / "config/routing-rules.md"
EMAIL_CAPTURE_CFG    = _ROOT / "config/email-capture.yaml"
LOG_PATH             = _ROOT / "logs/email-backfill.log"
DASHBOARD_URL        = "http://localhost:7777/warmup"

CLAUDE_MODEL   = "claude-sonnet-4-6"
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

GMAIL_API      = "https://gmail.googleapis.com/gmail/v1/users/me"

# ── Entity normalizer ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _entity_normalizer import EntityNormalizer as _EN  # noqa: E402
    _NORMALIZER: "_EN | None" = None

    def _get_normalizer() -> "_EN":
        global _NORMALIZER
        if _NORMALIZER is None:
            _NORMALIZER = _EN()
        return _NORMALIZER

    def _normalize_email_extraction_in_place(data: dict, norm: "_EN", stats: dict) -> None:
        stats.setdefault("entity_phonetic", 0)
        stats.setdefault("entity_canonical_match", 0)
        stats.setdefault("entity_unresolved_vague", 0)
        stats.setdefault("entity_corrections_log", [])

        def _resolve(v: str) -> str:
            if not v or not isinstance(v, str):
                return v
            m = norm.match(v)
            if m.source == "phonetic":
                stats["entity_phonetic"] += 1
                stats["entity_corrections_log"].append(f'"{m.original}" → "{m.canonical}" (phonetic)')
                return m.canonical
            if m.source in ("lp", "deal", "pipeline_target") and m.confidence in ("exact", "substring"):
                if m.canonical != m.original:
                    stats["entity_canonical_match"] += 1
                    stats["entity_corrections_log"].append(
                        f'"{m.original}" → "{m.canonical}" ({m.source}/{m.confidence})'
                    )
                return m.canonical
            if norm.is_vague(v):
                stats["entity_unresolved_vague"] += 1
                return f"[Unresolved — needs name] {v}"
            return v

        for item in data.get("envelope_items", []) or []:
            if isinstance(item, dict) and item.get("counterparty"):
                item["counterparty"] = _resolve(item["counterparty"])
        for contact in data.get("new_contacts", []) or []:
            if isinstance(contact, dict):
                if contact.get("name"): contact["name"] = _resolve(contact["name"])
                if contact.get("firm"): contact["firm"] = _resolve(contact["firm"])

    _NORMALIZER_AVAILABLE = True
except Exception:
    _NORMALIZER_AVAILABLE = False
    def _get_normalizer(): return None  # type: ignore[misc]
    def _normalize_email_extraction_in_place(*_a, **_kw): pass  # type: ignore[misc]


# ── Config file ───────────────────────────────────────────────────────────────

def _load_capture_config() -> dict:
    """Parse config/email-capture.yaml. Tiny hand-rolled YAML reader so we
    don't force a PyYAML dep — the file is intentionally flat and simple."""
    defaults = {
        "capture_label":            "dashboard-capture",
        "capture_queries":          [],
        "parent_hints":             [],
        "first_run_lookback_days":  180,
        "poll_lookback_days":       2,
        "max_threads_per_run":      40,
        "max_body_chars":           32000,
    }
    if not EMAIL_CAPTURE_CFG.exists():
        return defaults
    cfg: dict = dict(defaults)
    parent_hints: list[dict] = []
    capture_queries: list[str] = []
    cur_hint: dict | None = None
    in_hints = False
    in_queries = False
    for raw in EMAIL_CAPTURE_CFG.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Section headers
        if line.startswith("parent_hints:"):
            if cur_hint is not None:
                parent_hints.append(cur_hint); cur_hint = None
            in_hints, in_queries = True, False
            continue
        if line.startswith("capture_queries:"):
            if cur_hint is not None:
                parent_hints.append(cur_hint); cur_hint = None
            in_hints, in_queries = False, True
            continue
        # parent_hints rows
        if in_hints and line.startswith("  - "):
            if cur_hint is not None:
                parent_hints.append(cur_hint)
            cur_hint = {}
            body = line[4:]
            if ":" in body:
                k, _, v = body.partition(":")
                cur_hint[k.strip()] = v.strip().strip('"')
            continue
        if in_hints and line.startswith("    ") and cur_hint is not None and ":" in line:
            k, _, v = line.strip().partition(":")
            cur_hint[k.strip()] = v.strip().strip('"')
            continue
        # capture_queries bare-string list
        if in_queries and line.startswith("  - "):
            q = line[4:].strip()
            # Strip surrounding quotes if present (single or double)
            if len(q) >= 2 and q[0] in ('"', "'") and q[-1] == q[0]:
                q = q[1:-1]
            if q:
                capture_queries.append(q)
            continue
        # Leaving a list section
        if (in_hints or in_queries) and not line.startswith(" "):
            if cur_hint is not None:
                parent_hints.append(cur_hint); cur_hint = None
            in_hints = in_queries = False
        # Top-level scalars
        if not in_hints and not in_queries and ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip().strip('"')
            if k in ("first_run_lookback_days", "poll_lookback_days",
                     "max_threads_per_run", "max_body_chars"):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    pass
            elif k in ("capture_label",):
                cfg[k] = v
    if cur_hint is not None:
        parent_hints.append(cur_hint)
    cfg["parent_hints"] = parent_hints
    cfg["capture_queries"] = capture_queries
    return cfg


# ── Routing preamble ──────────────────────────────────────────────────────────

def _load_routing_rules() -> str:
    try:
        return ROUTING_RULES_PATH.read_text()
    except Exception as e:
        print(f"  [routing] WARN — could not load {ROUTING_RULES_PATH}: {e}",
              file=sys.stderr)
        return ""


# ── Auth ──────────────────────────────────────────────────────────────────────

def _load_pickle_creds():
    import pickle
    with open(GMAIL_TOKEN_PATH, "rb") as f:
        return pickle.load(f)


def _save_pickle_creds(creds):
    import pickle
    with open(GMAIL_TOKEN_PATH, "wb") as f:
        pickle.dump(creds, f)


def get_token() -> str:
    """Return a valid Gmail access token. Raises FileNotFoundError if the
    bootstrap script hasn't been run."""
    if not GMAIL_TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"{GMAIL_TOKEN_PATH} missing — run "
            "~/dashboards/scripts/bootstrap-gmail-auth.sh first."
        )
    from google.auth.transport.requests import Request
    creds = _load_pickle_creds()
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_pickle_creds(creds)
    return creds.token


# ── Gmail API helpers ─────────────────────────────────────────────────────────

def _get_json(url: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def list_labels(token: str) -> list[dict]:
    return _get_json(f"{GMAIL_API}/labels", token).get("labels", [])


def resolve_labels(token: str, capture_label: str) -> dict[str, dict]:
    """Return {label_name: label_dict} for the main label + all sub-labels
    whose name starts with 'capture_label/'. Gmail labels are user-created
    server-side — we don't create them."""
    all_labels = list_labels(token)
    prefix = f"{capture_label}/"
    wanted = {}
    for lbl in all_labels:
        name = lbl.get("name", "")
        if name == capture_label or name.startswith(prefix):
            wanted[name] = lbl
    return wanted


def list_threads(token: str, label_ids: list[str], query: str = "",
                 max_results: int = 40) -> list[dict]:
    """List threads matching a labelIds filter. Gmail caps page size at 500;
    we cap per-run at max_results to guard against label explosions."""
    threads: list[dict] = []
    page_token = None
    while True:
        params = {"maxResults": str(min(100, max_results - len(threads)))}
        for lid in label_ids:
            # Gmail accepts repeated labelIds query params
            params.setdefault("labelIds", lid)
        qs = []
        for lid in label_ids:
            qs.append(("labelIds", lid))
        qs.append(("maxResults", str(min(100, max_results - len(threads)))))
        if query:
            qs.append(("q", query))
        if page_token:
            qs.append(("pageToken", page_token))
        url = f"{GMAIL_API}/threads?" + urllib.parse.urlencode(qs)
        data = _get_json(url, token)
        threads.extend(data.get("threads", []))
        page_token = data.get("nextPageToken")
        if not page_token or len(threads) >= max_results:
            break
    return threads[:max_results]


def get_thread(token: str, thread_id: str) -> dict:
    return _get_json(f"{GMAIL_API}/threads/{thread_id}?format=full", token, timeout=30)


# ── Body + attachment extraction ──────────────────────────────────────────────

def _decode_body(data: str) -> str:
    """Gmail API returns body.data base64url-encoded."""
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload: dict, out_body: list[str], out_html: list[str],
                out_attachments: list[dict]) -> None:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    filename = payload.get("filename") or ""
    if filename and body.get("attachmentId"):
        out_attachments.append({
            "filename":      filename,
            "mimeType":      mime,
            "size":          body.get("size", 0),
            "attachment_id": body.get("attachmentId"),
        })
    elif mime == "text/plain" and body.get("data"):
        out_body.append(_decode_body(body["data"]))
    elif mime == "text/html" and body.get("data"):
        out_html.append(_decode_body(body["data"]))
    for p in payload.get("parts", []) or []:
        _walk_parts(p, out_body, out_html, out_attachments)


def _strip_html(s: str) -> str:
    s = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>",  "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>",   "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>",   "", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&amp;",  "&", s)
    s = re.sub(r"&lt;",   "<", s)
    s = re.sub(r"&gt;",   ">", s)
    s = re.sub(r"&quot;", '"', s)
    s = re.sub(r"&#39;",  "'", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_thread_content(thread: dict, max_body_chars: int) -> dict:
    """Return a normalized thread view: subject, participants, messages
    (date/from/to/body), attachments[]."""
    messages = thread.get("messages", []) or []
    parsed_msgs = []
    all_attachments: list[dict] = []
    subject = ""
    for msg in messages:
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        if not subject:
            subject = headers.get("subject", "")
        body_parts: list[str] = []
        html_parts: list[str] = []
        atts: list[dict] = []
        _walk_parts(msg.get("payload", {}), body_parts, html_parts, atts)
        body = "\n".join(body_parts).strip()
        if not body and html_parts:
            body = _strip_html("\n".join(html_parts))
        for a in atts:
            a["message_id"] = msg.get("id")
            all_attachments.append(a)
        parsed_msgs.append({
            "id":      msg.get("id"),
            "date":    headers.get("date", ""),
            "from":    headers.get("from", ""),
            "to":      headers.get("to", ""),
            "cc":      headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "body":    body[:max_body_chars],
        })
    # Concatenate messages oldest-first for the LLM (Gmail returns in that order).
    combined = []
    for m in parsed_msgs:
        combined.append(
            f"───── Message {m['id']} ─────\n"
            f"Date: {m['date']}\nFrom: {m['from']}\nTo: {m['to']}\n"
            + (f"Cc: {m['cc']}\n" if m['cc'] else "")
            + f"Subject: {m['subject']}\n\n{m['body']}"
        )
    full_text = "\n\n".join(combined)
    if len(full_text) > max_body_chars:
        full_text = full_text[:max_body_chars] + "\n\n[... truncated ...]"
    return {
        "subject":     subject,
        "messages":    parsed_msgs,
        "attachments": all_attachments,
        "full_text":   full_text,
        "message_count": len(parsed_msgs),
        "latest_from":  parsed_msgs[-1]["from"] if parsed_msgs else "",
        "latest_date":  parsed_msgs[-1]["date"] if parsed_msgs else "",
    }


# ── Dedup ─────────────────────────────────────────────────────────────────────

def load_dedup() -> dict:
    if DEDUP_PATH.exists():
        try:
            return json.loads(DEDUP_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_dedup(tracker: dict) -> None:
    DEDUP_PATH.write_text(json.dumps(tracker, indent=2))


def _thread_signature(thread: dict) -> str:
    """Stable signature for a thread at its current state — history-id of
    the newest message. If the latest message hasn't changed, the signature
    won't change either. historyId is present at thread and message level."""
    msgs = thread.get("messages") or []
    if msgs:
        return str(msgs[-1].get("historyId") or msgs[-1].get("id") or "")
    return str(thread.get("historyId") or thread.get("id") or "")


# ── Parent hint resolution ────────────────────────────────────────────────────

def parent_hint_from_labels(thread_label_ids: list[str],
                            label_map: dict[str, dict],
                            parent_hints: list[dict]) -> dict:
    """If any of the thread's labels match a sub-label with a configured
    parent_id, return that hint. Empty dict if nothing matches."""
    id_to_name = {l["id"]: name for name, l in label_map.items()}
    thread_label_names = {id_to_name[lid] for lid in thread_label_ids
                          if lid in id_to_name}
    for hint in parent_hints:
        if hint.get("label") in thread_label_names:
            return {
                "parent_id":  hint.get("parent_id") or "",
                "workstream": hint.get("workstream") or "",
                "label":      hint.get("label"),
            }
    return {}


# ── Claude extraction ─────────────────────────────────────────────────────────

_EMAIL_BODY = f"""

OWNER ATTRIBUTION FOR EMAIL — critical:
- "{_PRINCIPAL_FIRST}@" / "{_PRINCIPAL_EMAIL}" is always {_PRINCIPAL_FIRST}.
- If a non-{_PRINCIPAL_FIRST} sender says "I will send you X" / "I'll connect you with Y" / "We'll follow up with Z" → that is an awaiting_external item. owner="external", counterparty="<Firm> — <Person Name from From: header>".
- If {_PRINCIPAL_FIRST} (in a sent message) says "I will send you X" / "let me confirm Y" → owner="{_PRINCIPAL_FIRST}", content_type="my_action".
- "Let's schedule a call" / "let's find a time" where the other party is expected to propose times → awaiting_external (they owe the scheduling proposal). If {_PRINCIPAL_FIRST} offered times → my_action.
- A question from the other party that {_PRINCIPAL_FIRST} has not answered yet → my_action for {_PRINCIPAL_FIRST} ("respond to X re: Y").
- MUTUAL ACCEPTANCE REQUIRED: If {_PRINCIPAL_FIRST} (or the other party) merely OFFERS to do something ("I could ping X if useful" / "happy to send Y if helpful") and the counterparty does NOT affirmatively accept in a later reply in the same thread, it is NOT an action — DO NOT emit a my_action or awaiting_external for it. Offers the other side ignores or brushes past are conversational texture, not commitments. When in doubt whether acceptance happened, EXCLUDE.

COUNTERPARTY IDENTIFICATION — primary deal contact, not the emailer:
- The counterparty for an awaiting_external item is the firm/person WHOSE COMMITMENT YOU ARE TRACKING, not necessarily who is in the From: header.
- If a thread involves multiple non-{_PRINCIPAL_FIRST} parties (e.g. a deal contact CC'd alongside a banker or advisor), use the party who made the commitment as the counterparty — the deal-firm's primary contact, not the facilitating banker who forwarded the thread.
- If the thread is clearly about a named deal from the PIPELINE CONTEXT block, the counterparty must reference that deal's firm/person, not any intermediary CC'd on the email.
- NEVER use an LP name as the counterparty for a deal operational commitment. LP names belong in lp_intel items only.

PER-THREAD DEDUPLICATION — one item per distinct commitment:
- Within this email thread, if the same commitment appears across multiple messages (same actor + same deliverable + same underlying ask), emit it ONCE as the most specific/complete version. Do not emit paraphrases of the same action as separate items.
- The same "send X" appearing in 3 messages = 1 awaiting_external item, not 3.
- Use the most recent message's version of the commitment (latest stated deadline, most complete description).

STALENESS FILTER — do not emit items about past one-time events:
- If the email is about a conference, summit, forum, symposium, registration, RSVP, or attendance at a specific event, AND that event's date has already passed as of TODAY, do NOT emit an awaiting_external item for it. The opportunity is gone.
- Similarly, if the item is a scheduling proposal (propose times, send calendar invite, pick a slot) and the proposed date has clearly already passed, omit it.
- When in doubt about whether an event is past or upcoming, emit the item (false positives on suppression are worse than false positives on extraction).

TIME-REFERENCE NORMALIZATION — never emit floating phrases like "next week", "early next week", "later this week", "end of the week", or "end of next week" in `content` / `context`. They go silently stale once the date passes. Always materialize to "week of YYYY-MM-DD" using the email's send date as the anchor (snap forward to the Monday on/after the implied target). Same rule for "tomorrow" → explicit YYYY-MM-DD; "later today" → explicit ISO datetime. The compile layer also normalizes after the fact; doing it here keeps the data clean at write time. Codified in dash_corrections.md (2026-05-04).

ACTION-DIRECTION INVERSION CHECK (rule Y2) — when the action verb is a transmission verb (`send`, `share`, `deliver`, `forward`, `provide`, `transmit`, `circulate`, `pass along`), explicitly identify which side is the sender by inspecting the email's From/To and role context BEFORE emitting the item:
- Investment banks / placement agents / fundraising advisors pitching deal flow or capital to {_PRINCIPAL_FIRST} → THEY send teasers/CIMs/data rooms/term sheets TO {_PRINCIPAL_FIRST}. Counterparty owns the action; emit `state: waiting`, `owner: external`, `counterparty: "Firm — Person"`. {_PRINCIPAL_FIRST} RECEIVES; do NOT emit a my_action telling {_PRINCIPAL_FIRST} to "send" what is being pitched IN. Use From/To headers and signature blocks as the primary signal of who is sending.
- Principal sponsoring a deal to LPs / co-investors / lenders → PRINCIPAL sends materials. Emit `owner: <one of {_OWNERS_PIPE}>`, `state: active`.
- Mutual exchanges (NDAs, term sheets, mark-ups, redlines passed back and forth) → emit two envelope_items, one per direction, each with the correct owner.
- Default if unclear: emit as `state: waiting` with the counterparty as owner — better to under-attribute to the principal than fabricate a send-verb on the wrong side. The Astris-flip failure mode (codified 2026-05-04) was a fundraising advisor pitching IN that was wrongly written as {_PRINCIPAL_FIRST} owing the send.

COUNTERPARTY PLACEHOLDERS — when the firm cannot be identified from the email, emit `counterparty: ""`. NEVER emit a generic placeholder like "assistant", "attorneys", "Unknown", "team", a bare email address, or a person's name with no firm context. Bare email addresses and orphan dashes ("— {_DEAL_LEAD_NAME}") make the by-firm grouping fragment and the dashboard surface noisier than it has to be.

EXTRACTION — respond ONLY with valid JSON, no markdown fences.

Required fields:

1. one_line_summary: under 25 words — lead with the so-what for a senior investor. Name the deal/firm and the ask.

2. envelope_items: array of envelope-shaped objects as defined in the ENVELOPE ROUTING RULES block. For every item:
   - due dates default to 7 days from TODAY if the email doesn't specify; use the email's stated date otherwise.
   - counterparty MUST be "<Firm> — <Person Name>" when owner=external.
   - parent_id: if the email is clearly about a named deal / LP already in pipeline context, set the slug. If the email is tagged with a capture sub-label hint, prefer that hint's parent_id (provided in the dynamic block).
   - content: verb-first for actions, specific for takeaways. Never "follow up" without a named party + specific content.

   STRICT RULES (enforced at write time — items failing these go to routingExceptions[] and are LOST from the dashboard):
   • content_type="status_update" REQUIRES a non-empty parent_id (deal ticker or LP slug). If you cannot identify the parent, emit deal_takeaway instead.
   • content_type="lp_intel" REQUIRES parent_id (LP slug).
   • owner whitelist: {_OWNERS_QUOTED}. Common variant spellings (full name, nickname, alternate spelling) are normalized automatically; firm names as owner are rejected — use owner="external" + counterparty="Firm — Person" instead.

3. new_contacts: array of {{name, firm, title, email, context, confidence}} for every person named in the thread — senders, recipients, people mentioned in-body. email may be blank if not known.

   CONFIDENCE GUIDE (codified 2026-05-04 — drives auto-promotion of new firms to fundraising/deal config):
     • "high"   → Named with role/firm AND co-occurs with action verb (commit, send, schedule, intro, decided) OR is the principal counterparty in the thread.
     • "medium" → Named clearly but only context, no commitment.
     • "low"    → Passing mention; could be a peer/competitor or background reference rather than an actionable contact.
   Default: "medium". Compile uses confidence to gate auto-promotion — `low` mentions are kept in a triage queue, not auto-added to dashboard surfaces.

4. attachments_note: one-line human-readable description of what attachments are present (e.g. "Business plan PDF + term sheet"). DO NOT attempt to summarize attachment contents — you have only the filename/mime. If no attachments, return empty string.

5. mentioned_firms: Array of every firm/organization name surfaced in the thread — actionable AND passing references. Powers the inverse-audit ("what's mentioned but not on the dashboard?") sweep. NEVER omit firms just because they didn't generate an envelope item.
   Each: {{"name": "...", "context": "one-phrase: what role did they play in the thread?"}}

STATE FIELD on envelope_items (codified 2026-05-04 — eliminates compile-time inference of state from prose):
   On every envelope_item where content_type implies an action (`my_action`, `awaiting_external`, `status_update`), include a `state` field:
     • "active"   → Principal/team owns the next move.
     • "waiting"  → Counterparty owes the next move (default for awaiting_external).
     • "watching" → Passive intel; no near-term action.
     • "blocked"  → Gated on a known dependency. Name the gate in `content`.
     • "dormant"  → Relationship paused; reactivate only on fresh signal.
     • "closed"   → Resolved. When state="closed", populate `resolution_source` field (doc title + date OR follow-up [RESOLVED] tag OR specific event evidence).

If {_PRINCIPAL_FIRST} is not a participant (the message is a forwarded blast, newsletter, or listserv post), emit ONLY deal_takeaway / origination_idea / theme_note items and no my_action / awaiting_external. Default-exclude if in doubt.

6. deal_log_entries: Array. For each ACTIVE DEAL listed in the DEAL PIPELINE TARGETS block above that this email SUBSTANTIVELY touches (not a passing mention), emit ONE entry per deal:
   {{"deal_id": "<slug exactly as given in the DEAL PIPELINE TARGETS block>", "summary": "<≤25 words: what happened on this deal — who said what, what moved, what stalled>", "evidence": "<≤100 chars verbatim quote from the email anchoring this entry>"}}
   Skip the deal entirely if it was only mentioned in passing (no decision, no data point, no action, no commitment). High-precision tagging — better to omit than to over-attribute. Codified 2026-05-04 (rule V1+).

RESPOND ONLY with:
{{"one_line_summary":"...","envelope_items":[{{"content_type":"...","owner":"...","counterparty":"","parent_id":"","due":"","state":"","resolution_source":"","context":"...","dashboard_path":"...","content":"..."}}],"new_contacts":[{{"name":"...","firm":"...","title":"...","email":"","context":"...","confidence":"high|medium|low"}}],"mentioned_firms":[{{"name":"...","context":"..."}}],"attachments_note":"...","deal_log_entries":[{{"deal_id":"","summary":"","evidence":""}}]}}
"""

EMAIL_PREAMBLE = _fc.build_email_header(_CTX) + _EMAIL_BODY


def _load_pipeline_context() -> str:
    """Load a compact cross-reference of deal targets + LPs the extractor
    should recognize. Same pattern as cos_otter_backfill.load_pipeline_context —
    kept tiny here to avoid cross-imports."""
    try:
        data = json.loads(PIPELINE_DATA_PATH.read_text())
    except Exception:
        return ""
    # Look up by canonical "deals" key first, then fall back to a
    # workstream-keyed bucket (legacy data files keyed deals under the
    # firm-specific workstream slug — kept for shape-compat).
    workstream_key = (_DEAL_WS or "").lower().split()[0] if _DEAL_WS else ""
    deals = (
        data.get("deals")
        or (data.get(workstream_key) if workstream_key else None)
        or []
    )
    lines = [f"{_PIPELINE_BLOCK_LABEL} (name → slug):"]
    for d in deals[:80]:
        slug = d.get("ticker") or d.get("id") or d.get("slug") or ""
        name = d.get("name") or slug
        if slug and name:
            lines.append(f"  - {name} → {slug}")
    lps = data.get("lps") or data.get("lpData") or []
    if lps:
        lines.append("\nLP TARGETS (name → slug):")
        for l in lps[:80]:
            slug = l.get("id") or l.get("slug") or ""
            name = l.get("name") or slug
            if slug and name:
                lines.append(f"  - {name} → {slug}")
    return "\n".join(lines)


def call_claude(thread_view: dict, parent_hint: dict,
                pipeline_context: str) -> dict:
    """Run the four-block cached prompt against Sonnet."""
    today = datetime.now().strftime("%Y-%m-%d")
    default_due = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    atts_brief = "\n".join(
        f"  - {a['filename']} ({a['mimeType']}, {a.get('size', 0)} bytes)"
        for a in thread_view["attachments"]
    ) or "  (none)"

    hint_line = ""
    if parent_hint:
        hint_line = (
            f"PARENT HINT FROM LABEL {parent_hint.get('label')}: "
            f"parent_id={parent_hint.get('parent_id') or '(unset)'} / "
            f"workstream={parent_hint.get('workstream') or '(unset)'}\n"
            "Prefer this parent_id on deal-related items unless the thread "
            "content clearly contradicts it.\n\n"
        )

    dynamic = (
        f"TODAY: {today}\n"
        f"DEFAULT DUE (7d out, if the email doesn't state one): {default_due}\n"
        f"THREAD SUBJECT: {thread_view['subject']}\n"
        f"MESSAGE COUNT: {thread_view['message_count']}\n"
        f"LATEST FROM: {thread_view['latest_from']}\n"
        f"LATEST DATE: {thread_view['latest_date']}\n"
        f"{hint_line}"
        f"ATTACHMENTS:\n{atts_brief}\n\n"
        f"THREAD CONTENT (oldest message first):\n\n{thread_view['full_text']}"
    )

    routing_rules = _load_routing_rules()
    content: list[dict] = []
    if routing_rules:
        content.append({
            "type": "text",
            "text": "ENVELOPE ROUTING RULES (shared contract — see config/routing-rules.md):\n\n"
                    + routing_rules,
            "cache_control": {"type": "ephemeral"},
        })
    content.append({
        "type": "text",
        "text": EMAIL_PREAMBLE,
        "cache_control": {"type": "ephemeral"},
    })
    if pipeline_context:
        content.append({
            "type": "text",
            "text": pipeline_context,
            "cache_control": {"type": "ephemeral"},
        })
    content.append({"type": "text", "text": dynamic})

    # Auth-mode aware dispatch (codified 2026-05-05). subscription path
    # bills against Pro/Max OAuth; api path is the legacy urllib POST.
    import _claude_dispatch  # noqa: PLC0415
    raw = _claude_dispatch.call(
        task_type="cos_email_backfill",
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
        api_timeout=90,
    ).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ── Envelope writer shim ──────────────────────────────────────────────────────

def _load_envelope_writer():
    spec = importlib.util.spec_from_file_location(
        "_envelope_writer", str(_HERE / "_envelope_writer.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Per-thread processing ─────────────────────────────────────────────────────

def process_thread(token: str, thread_meta: dict, label_map: dict,
                   parent_hints: list[dict], pipeline_context: str,
                   cfg: dict, stats: dict, dry_run: bool = False) -> dict | None:
    tid = thread_meta["id"]
    try:
        thread = get_thread(token, tid)
    except Exception as e:
        print(f"  ERROR fetching thread {tid}: {e}", file=sys.stderr)
        return None

    msgs = thread.get("messages") or []
    if not msgs:
        return None
    signature = _thread_signature(thread)

    view = extract_thread_content(thread, cfg["max_body_chars"])
    subject = view["subject"] or "(no subject)"

    # Parent hint from thread's label set (intersect with configured hints)
    label_ids = set()
    for m in msgs:
        for lid in m.get("labelIds") or []:
            label_ids.add(lid)
    hint = parent_hint_from_labels(sorted(label_ids), label_map, parent_hints)

    print(f"  ▶ {tid[:16]}… subject={subject[:70]!r}  msgs={len(msgs)}  "
          f"atts={len(view['attachments'])}"
          + (f"  hint={hint.get('parent_id')}" if hint.get("parent_id") else ""),
          flush=True)

    if dry_run:
        return {"signature": signature, "subject": subject, "dry_run": True}

    if len(view["full_text"].strip()) < 80:
        print(f"    too short ({len(view['full_text'])} chars) — skipping",
              flush=True)
        return {"signature": signature, "subject": subject, "skipped": "too_short"}

    _norm = _get_normalizer()
    if _norm is not None:
        try:
            corrected, _phon = _norm.apply_phonetic(view["full_text"])
            if _phon:
                view = dict(view)
                view["full_text"] = corrected
                stats.setdefault("phonetic_corrections", []).extend(_phon)
                print(f"    🔤  Phonetic corrections applied: {_phon}", flush=True)
        except Exception as _pe:
            print(f"    ⚠️   Phonetic normalization failed: {_pe}", file=sys.stderr)

    try:
        data = call_claude(view, hint, pipeline_context)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"    Claude HTTP {e.code}: {body[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    Claude call failed: {e}", file=sys.stderr)
        return None

    if _norm is not None:
        try:
            _normalize_email_extraction_in_place(data, _norm, stats)
            _ent_n = (stats.get("entity_phonetic", 0) + stats.get("entity_canonical_match", 0) +
                      stats.get("entity_unresolved_vague", 0))
            if _ent_n:
                print(f"    🧭  Entity reconciliation: {_ent_n} adjustments", flush=True)
        except Exception as _ne:
            print(f"    ⚠️   Entity reconciliation failed: {_ne}", file=sys.stderr)

    envelope_items = data.get("envelope_items") or []
    summary = (data.get("one_line_summary") or "").strip()
    contacts = data.get("new_contacts") or []
    atts_note = (data.get("attachments_note") or "").strip()

    # Apply parent hint defensively — if the extractor left parent_id blank
    # on a deal-related item but the label pinned one, fill it in.
    if hint.get("parent_id"):
        for e in envelope_items:
            if not (e.get("parent_id") or "").strip() and e.get("content_type") in (
                    "deal_takeaway", "status_update", "origination_idea"):
                e["parent_id"] = hint["parent_id"]

    # Source_ref — never includes attachment bytes, only metadata.
    thread_url = f"https://mail.google.com/mail/u/0/#all/{tid}"
    att_meta = [{
        "filename":      a["filename"],
        "mimeType":      a["mimeType"],
        "size":          a.get("size", 0),
        "attachment_id": a["attachment_id"],
        "message_id":    a["message_id"],
        # Explicitly NOT downloaded — see --download-approved helper.
        "downloaded":    False,
    } for a in view["attachments"]]
    src_ref = {
        "type":         "email",
        "title":        subject,
        "doc_url":      thread_url,
        "date":         _parse_date_header(view["latest_date"]),
        "thread_id":    tid,
        "from":         view["latest_from"],
        "attachments":  att_meta,
    }
    for e in envelope_items:
        e.setdefault("source_ref", src_ref)

    routed = {"routed": {}, "exceptions": 0, "skipped_dupes": 0}
    if envelope_items:
        try:
            ew = _load_envelope_writer()
            routed = ew.append_items(envelope_items)
            rtotal = sum(routed.get("routed", {}).values())
            print(f"    routed {rtotal} items; exceptions={routed.get('exceptions', 0)} "
                  f"→ {routed.get('routed', {})}", flush=True)
        except Exception as e:
            print(f"    envelope writer failed: {e}", file=sys.stderr)

    # Sidecar tap for deal_log_entries[] — bypasses _envelope_writer
    # (parallel-session-owned) and feeds the V1+ Pass A0 lookup at
    # compile time. Soft-fails so it never blocks extraction.
    try:
        import _deal_log_sidecar as _dls
        _dle = data.get("deal_log_entries", []) or []
        if _dle:
            _n = _dls.append(_dle, src_ref, source_id=str(tid))
            if _n:
                print(f"    deal-log sidecar: +{_n} entries", flush=True)
    except Exception as _se:
        print(f"    deal-log sidecar skipped (non-critical): {_se}",
              file=sys.stderr)

    stats["followups_added"] += routed.get("routed", {}).get("my_action", 0)
    stats["contacts_added"]  += len(contacts)
    stats["processed"]       += 1
    if summary:
        print(f"    summary: {summary}", flush=True)
    if atts_note:
        print(f"    attachments: {atts_note}", flush=True)

    return {
        "signature":    signature,
        "subject":      subject,
        "summary":      summary,
        "routed":       routed.get("routed", {}),
        "n_contacts":   len(contacts),
        "n_attachments": len(att_meta),
    }


def _parse_date_header(date_str: str) -> str:
    """Best-effort RFC-2822 → YYYY-MM-DD. Falls back to today on failure."""
    from email.utils import parsedate_to_datetime
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


# ── Download-approved helper (gated) ──────────────────────────────────────────

def download_approved(token: str, message_id: str) -> None:
    """Print what would be downloaded, then download each attachment to
    ~/dashboards/data/email-attachments/<message_id>/. This is the only
    code path in this file that writes attachment bytes to disk, and it
    requires the --download-approved CLI flag with a specific message_id.
    Per ~/.claude/CLAUDE.md safety rules, attachments are never
    auto-fetched."""
    msg = _get_json(f"{GMAIL_API}/messages/{message_id}?format=full", token)
    atts: list[dict] = []
    _walk_parts(msg.get("payload", {}), [], [], atts)
    print(f"Message {message_id}: {len(atts)} attachment(s)")
    if not atts:
        return
    out_dir = _ROOT / "data" / "email-attachments" / message_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for a in atts:
        print(f"  downloading {a['filename']} ({a['mimeType']}, {a.get('size', 0)} bytes)")
        url = f"{GMAIL_API}/messages/{message_id}/attachments/{a['attachment_id']}"
        data = _get_json(url, token, timeout=60)
        raw = data.get("data", "")
        padded = raw + "=" * (-len(raw) % 4)
        blob = base64.urlsafe_b64decode(padded)
        dest = out_dir / a["filename"]
        dest.write_bytes(blob)
        print(f"    → {dest}")


# ── Main ──────────────────────────────────────────────────────────────────────

def _ensure_log_dir() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _append_log(line: str) -> None:
    _ensure_log_dir()
    with open(LOG_PATH, "a") as f:
        f.write(line.rstrip() + "\n")


def main():
    p = argparse.ArgumentParser(description="COS email backfill (dashboard-capture label)")
    p.add_argument("--list",     action="store_true", help="Enumerate threads; no processing")
    p.add_argument("--backfill", action="store_true", help="Force the first-run window")
    p.add_argument("--force",    action="store_true", help="Re-process threads already in dedup")
    p.add_argument("--id", dest="thread_id", help="Limit to one thread id (implies --force)")
    p.add_argument("--since", help="Override Gmail q= date filter (YYYY-MM-DD)")
    p.add_argument("--config-dir", dest="config_dir",
                   help="Tenant config directory (overrides $COS_CONFIG_DIR; "
                        "default ~/cos-pipeline-config-<slug>/). Honored at "
                        "module-load via _peek_config_dir().")
    p.add_argument("--print-prompt", action="store_true",
                   help="Print the rendered LLM preamble (header + body) and exit. "
                        "Useful for verifying tenant-config interpolation.")
    p.add_argument("--download-approved", dest="download_approved",
                   help="Download attachments from a specific message_id (gated action)")
    args = p.parse_args()

    # Diagnostic: render the cached preamble and exit. Does not touch Gmail.
    if args.print_prompt:
        print("=== EMAIL_PREAMBLE (header + body) ===")
        print(EMAIL_PREAMBLE)
        print("=== END ===")
        return

    cfg = _load_capture_config()

    # Auth
    try:
        token = get_token()
    except FileNotFoundError as e:
        msg = f"BOOTSTRAP REQUIRED: {e}"
        print(msg, file=sys.stderr)
        _append_log(f"{datetime.now().isoformat()} {msg}")
        sys.exit(2)
    except Exception as e:
        print(f"Gmail auth failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Gated attachment download path
    if args.download_approved:
        download_approved(token, args.download_approved)
        return

    print("=== COS Email Backfill ===", flush=True)
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print(f"Label:  {cfg['capture_label']}", flush=True)

    # Resolve labels
    try:
        label_map = resolve_labels(token, cfg["capture_label"])
    except Exception as e:
        print(f"Could not list labels: {e}", file=sys.stderr)
        sys.exit(1)
    main_label = label_map.get(cfg["capture_label"])
    capture_queries = list(cfg.get("capture_queries") or [])
    # Union with auto-generated queries from dashboard state (regenerated
    # daily by _capture_query_builder.py — new deals / LPs / frequent
    # senders flow in without manual yaml edits).
    auto_path = Path.home() / "dashboards/data/user-state/capture-queries.auto.json"
    auto_queries: list[str] = []
    if auto_path.exists():
        try:
            auto = json.loads(auto_path.read_text())
            auto_queries = list(auto.get("queries") or [])
            # Merge parent hints from auto layer (keyword → parent_id) into cfg
            auto_hints = auto.get("parent_hints_by_keyword") or {}
            if auto_hints:
                cfg.setdefault("parent_hints_auto", auto_hints)
            print(f"  auto queries (from dashboard state): {len(auto_queries)} "
                  f"(generated {auto.get('generated_at', '?')})", flush=True)
        except Exception as e:
            print(f"  [auto-queries] WARN could not load {auto_path}: {e}",
                  file=sys.stderr)
    # Dedupe yaml + auto preserving order (yaml wins for ordering)
    seen = set()
    merged_queries: list[str] = []
    for q in capture_queries + auto_queries:
        if q and q not in seen:
            seen.add(q)
            merged_queries.append(q)
    capture_queries = merged_queries
    if not main_label and not capture_queries:
        print(f"Label {cfg['capture_label']!r} not found and no capture_queries "
              "configured — nothing to do. Either create the label and apply "
              "it to threads, or populate capture_queries in "
              "config/email-capture.yaml.",
              flush=True)
        sys.exit(0)
    if not main_label:
        print(f"Label {cfg['capture_label']!r} not found — continuing with "
              f"{len(capture_queries)} capture_queries only.", flush=True)
    sub_labels = [name for name in label_map if name != cfg["capture_label"]]
    print(f"Sub-labels: {sub_labels or '(none)'}", flush=True)
    print(f"Capture queries: {len(capture_queries)}", flush=True)

    # Dedup
    tracker = load_dedup()
    force = args.force or bool(args.thread_id)
    is_first_run = len(tracker) == 0
    print(f"Dedup tracker: {len(tracker)} previously processed threads", flush=True)

    # Build q filter
    label_query = ""
    if args.since:
        label_query = f"after:{args.since.replace('-', '/')}"
    elif args.backfill or is_first_run:
        cutoff = (datetime.now() -
                  timedelta(days=cfg["first_run_lookback_days"])
                  ).strftime("%Y/%m/%d")
        label_query = f"after:{cutoff}"
        print(f"First-run / --backfill window: {label_query}", flush=True)

    # Poll window applied to capture_queries. Each query is narrow enough
    # on its own that re-scanning the archive every run would be wasteful;
    # poll_lookback_days gives a catch-up buffer across the 15-min cron.
    poll_cutoff = (datetime.now() -
                   timedelta(days=int(cfg.get("poll_lookback_days", 2)))
                   ).strftime("%Y/%m/%d")
    poll_scope = f"after:{poll_cutoff}"

    # Enumerate threads from all sources, then union by thread id
    collected: dict[str, dict] = {}
    max_per_run = int(cfg["max_threads_per_run"])
    try:
        # Scan the main capture_label AND every sub-label independently.
        # Gmail labels are orthogonal — a thread tagged only with e.g.
        # `dashboard-capture/<deal-slug>` does NOT appear under the bare
        # `dashboard-capture`. So we union results across
        # [main_label, *sub_labels] to ensure any sub-label tag alone is
        # sufficient to pull a thread in.
        label_scan_targets = []
        if main_label:
            label_scan_targets.append(main_label)
        for sl_name in sub_labels:
            sl = label_map.get(sl_name)
            if sl:
                label_scan_targets.append(sl)
        label_hits_total = 0
        for lbl in label_scan_targets:
            try:
                lt = list_threads(token, [lbl["id"]], query=label_query,
                                  max_results=max_per_run)
                for t in lt:
                    collected.setdefault(t["id"], t)
                if lt:
                    label_hits_total += len(lt)
                    print(f"  label [{lbl['name']}]: {len(lt)} matches",
                          flush=True)
            except Exception as e:
                print(f"  label [{lbl['name']}]: scan failed: {e}",
                      file=sys.stderr)
        print(f"  label matches: {label_hits_total}", flush=True)
    except Exception as e:
        print(f"Could not list label threads: {e}", file=sys.stderr)
    # Per-query scans — budget remaining slots across queries so a single
    # noisy one can't starve the others.
    remaining = max(0, max_per_run - len(collected))
    per_query_cap = max(5, remaining // max(1, len(capture_queries))) if capture_queries else 0
    for q in capture_queries:
        # On --backfill or first-run, widen to the label backfill window too;
        # otherwise stay within poll_scope.
        effective_q = f"{q} {label_query or poll_scope}".strip()
        try:
            qt = list_threads(token, [], query=effective_q,
                              max_results=per_query_cap)
            new_hits = sum(1 for t in qt if t["id"] not in collected)
            for t in qt:
                collected.setdefault(t["id"], t)
            print(f"  query [{q[:60]}{'…' if len(q) > 60 else ''}]: "
                  f"{len(qt)} matches, {new_hits} new", flush=True)
        except Exception as e:
            print(f"  query [{q[:60]}…]: failed ({type(e).__name__}: {e})",
                  file=sys.stderr)
        if len(collected) >= max_per_run:
            break
    threads = list(collected.values())

    if args.thread_id:
        threads = [t for t in threads if t["id"] == args.thread_id]
        if not threads:
            # Fall back to direct fetch — the thread may be outside the query window
            threads = [{"id": args.thread_id}]

    print(f"Matching threads: {len(threads)}", flush=True)

    if args.list or not threads:
        for t in threads:
            tid = t["id"]
            try:
                thr = get_thread(token, tid)
            except Exception as e:
                print(f"  {tid}  ERR {e}", flush=True)
                continue
            view = extract_thread_content(thr, cfg["max_body_chars"])
            sig = _thread_signature(thr)
            dedup_sig = tracker.get(tid, {}).get("signature", "")
            status = "NEW" if sig != dedup_sig else "unchanged"
            print(f"  [{status:9s}] {tid[:16]}  {view['subject'][:70]!r}  "
                  f"msgs={view['message_count']} atts={len(view['attachments'])}",
                  flush=True)
        if args.list:
            print("\n--list mode: exiting without processing.", flush=True)
            return

    # Note (2026-05-05): the legacy "ANTHROPIC_API_KEY required" guard
    # was removed when this script migrated to _claude_dispatch. The
    # shim handles the auth choice: in subscription mode it routes via
    # claude-agent-sdk → CLAUDE_CODE_OAUTH_TOKEN, no API key needed; in
    # api mode it requires ANTHROPIC_API_KEY and raises a clear error
    # if it's missing. Either way, we let the dispatch layer be the
    # single point of auth-availability enforcement.

    pipeline_context = _load_pipeline_context()
    if pipeline_context:
        print(f"Pipeline context loaded ({len(pipeline_context)} chars)", flush=True)

    stats = {
        "processed":       0,
        "skipped_dedup":   0,
        "errors":          0,
        "followups_added": 0,
        "contacts_added":  0,
    }

    for t in threads:
        tid = t["id"]
        try:
            # Quick signature check — avoid a full thread fetch + Claude call when
            # the latest message hasn't changed since last run.
            meta = get_thread(token, tid)
        except Exception as e:
            print(f"  ERROR fetching {tid}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        sig = _thread_signature(meta)
        prior = tracker.get(tid, {})
        if prior.get("signature") == sig and not force:
            stats["skipped_dedup"] += 1
            continue

        result = process_thread(token, {"id": tid}, label_map,
                                cfg["parent_hints"], pipeline_context, cfg, stats)
        if result is None:
            stats["errors"] += 1
            tracker[tid] = {
                "signature":     sig,
                "last_error_at": datetime.now().isoformat(),
                "subject":       (meta.get("messages", [{}])[-1].get("payload", {}).get("headers", []) or [{}])[0].get("value", "") if meta.get("messages") else "",
            }
        else:
            tracker[tid] = {
                "signature":     result["signature"],
                "processed_at":  datetime.now().isoformat(),
                "subject":       result.get("subject", ""),
            }
        save_dedup(tracker)

    # Warmup dashboard
    pinged = False
    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_URL, method="POST"), timeout=3,
        )
        pinged = True
        print("\n✅  Dashboard warmup triggered", flush=True)
    except Exception:
        print("\n⚠️   Dashboard not running (skipped warmup)", flush=True)

    # Run summary
    summary_line = (f"{stats['processed']} threads processed | "
                    f"{stats['followups_added']} follow-ups added | "
                    f"{stats['contacts_added']} contacts added | "
                    f"dashboard {'pinged' if pinged else 'skipped'}")
    print("\n" + "=" * 60, flush=True)
    print("RUN SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print(summary_line, flush=True)
    print(f"Skipped (dedup): {stats['skipped_dedup']}", flush=True)
    print(f"Errors:          {stats['errors']}", flush=True)

    _append_log(f"{datetime.now().isoformat()} {summary_line}")


if __name__ == "__main__":
    main()
