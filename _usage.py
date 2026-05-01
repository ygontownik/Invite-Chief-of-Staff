"""Shared Anthropic usage logger.

Appends one JSONL line per API call to ~/dashboards/data/anthropic-usage.jsonl.
Read by scripts/anthropic-usage-report.sh. Fails silently — never raises.

Import pattern from a sibling routines/ subdir:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _usage import log_usage
"""
import json
from datetime import datetime, timezone
from pathlib import Path

_LOG = Path.home() / "dashboards/data/anthropic-usage.jsonl"


def log_usage(site: str, model: str, resp: dict) -> None:
    try:
        u = resp.get("usage", {}) if isinstance(resp, dict) else {}
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a") as f:
            f.write(json.dumps({
                "ts":           datetime.now(timezone.utc).isoformat(),
                "site":         site,
                "model":        model,
                "in":           u.get("input_tokens", 0),
                "out":          u.get("output_tokens", 0),
                "cache_read":   u.get("cache_read_input_tokens", 0),
                "cache_create": u.get("cache_creation_input_tokens", 0),
            }) + "\n")
    except Exception:
        pass
