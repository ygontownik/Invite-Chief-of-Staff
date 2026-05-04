# server-routes.delta.md — Track E1 surgical patches to cos-dashboard-server.py

Source: `~/cos-pipeline/cos-dashboard-server.py` (3,764 lines, live).
Owner: Track E1 (parallel to Track C / fetch.py.next).
DO NOT write a full server.py.next — Phase 1.7 already produced one
for HTML serving (`cos-dashboard-server.py.next`). This file lists the
incremental route + config-path patches that should be folded into
that pending .next OR applied as a follow-on patch by the morning
review.

All line numbers verified against `cos-dashboard-server.py` on
2026-05-03. Patches are PROPOSED — do not apply tonight (HARD RULE C15).

---

## Patch 1 — rename `_TOMAC_CONFIG_PATH` constant (line 380)

REPLACE (line 379-380):
```
_RECRUIT_CONFIG_PATH = Path(__file__).parent.parent / 'config' / 'recruit-config.yaml'
_TOMAC_CONFIG_PATH   = Path(__file__).parent.parent / 'config' / 'tomac-config.yaml'
```
WITH:
```
_RECRUIT_CONFIG_PATH = Path(__file__).parent.parent / 'config' / 'recruit-config.yaml'

# Per-tenant deal config — primary source is the per-tenant config repo
# (~/cos-pipeline-config-<slug>/config/deal-config.yaml), with one-release
# fallback to the legacy ~/dashboards/config/tomac-config.yaml file.
def _resolve_deal_config_path():
    import os
    env = os.environ.get('COS_CONFIG_DIR')
    candidates = []
    if env:
        candidates.append(Path(env).expanduser() / 'config' / 'deal-config.yaml')
        candidates.append(Path(env).expanduser() / 'deal-config.yaml')
    candidates.append(Path.home() / 'cos-pipeline-config-tomac' / 'config' / 'deal-config.yaml')
    candidates.append(Path(__file__).parent.parent / 'config' / 'deal-config.yaml')
    # Legacy fallback (one-release back-compat — remove next major release)
    candidates.append(Path(__file__).parent.parent / 'config' / 'tomac-config.yaml')
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return candidates[-1]   # may not exist; loader will catch the error

_DEAL_CONFIG_PATH = _resolve_deal_config_path()
# Back-compat alias — legacy callers may still reference _TOMAC_CONFIG_PATH.
# Will be removed in the release after this one.
_TOMAC_CONFIG_PATH = _DEAL_CONFIG_PATH
```

## Patch 2 — rename `_load_tomac_config` (lines 399-412)

REPLACE (lines 399-412):
```
def _load_tomac_config() -> dict:
    """Load config/tomac-config.yaml and return as a plain dict for JSON injection."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(_TOMAC_CONFIG_PATH.read_text()) or {}
        return {
            'liveDeals':             raw.get('liveDeals', []),
            'dealOrigination':       raw.get('dealOrigination', []),
            'capitalRaisingAdvisors':raw.get('capitalRaisingAdvisors', []),
            'prospectiveInvestors':  raw.get('prospectiveInvestors', []),
        }
    except Exception as e:
        print(f'[tomac-config] load failed: {e}', flush=True)
        return {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [], 'prospectiveInvestors': []}
```
WITH:
```
def _load_deal_config() -> dict:
    """Load config/deal-config.yaml and return as a plain dict for JSON injection.
    Renamed from _load_tomac_config in PLAN E1.4. Falls back to the legacy
    tomac-config.yaml path for one release; see _resolve_deal_config_path()."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(_DEAL_CONFIG_PATH.read_text()) or {}
        return {
            'liveDeals':              raw.get('liveDeals', []),
            'dealOrigination':        raw.get('dealOrigination', []),
            'capitalRaisingAdvisors': raw.get('capitalRaisingAdvisors', []),
            'prospectiveInvestors':   raw.get('prospectiveInvestors', []),
        }
    except Exception as e:
        print(f'[deal-config] load failed: {e}', flush=True)
        return {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [], 'prospectiveInvestors': []}

# Back-compat alias — remove in next major release.
_load_tomac_config = _load_deal_config
```

## Patch 3 — JS injection variable (lines 911 + 926)

REPLACE (line 911):
```
        tomac_config = _load_tomac_config()
```
WITH:
```
        deal_config = _load_deal_config()
```

REPLACE (line 913):
```
        tomac_config = {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [], 'prospectiveInvestors': []}
```
WITH:
```
        deal_config = {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [], 'prospectiveInvestors': []}
```

REPLACE (line 926):
```
        'window.__TOMAC_CONFIG__ = ' + json.dumps(tomac_config) + ';'
```
WITH:
```
        # Canonical name — JS bundles should read window.__DEAL_CONFIG__.
        'window.__DEAL_CONFIG__ = ' + json.dumps(deal_config) + ';'
        # Back-compat alias — pre-rename React bundle still references
        # window.__TOMAC_CONFIG__. Leave for one release; remove after the
        # bundle in ~/dashboards/app/templates/* is rebuilt against __DEAL_CONFIG__.
        'window.__TOMAC_CONFIG__ = window.__DEAL_CONFIG__;'
```

## Patch 4 — consolidate routes: add `/deals` (already partially exists) + 301 `/tomac`

The live file already has `/deals`, `/deals/`, and `/deals/<rel>` handlers
(lines 1725-1740) AND a `/tomac` 301-redirect to `/tomac-cove/` (lines
1741-1746). Per PLAN E1.6 we want `/tomac` to redirect to `/deals/`
(the consolidated sourcing/pipeline view), not `/tomac-cove/`. Patch:

REPLACE (lines 1741-1746):
```
        elif self.path == '/tomac' or self.path == '/tomac/':
            # Legacy "Firm Pipeline" view retired 2026-04-27 — superseded by
            # /tomac-cove/ (live portfolio) and /deals/ (sourcing funnel).
            self.send_response(301)
            self.send_header('Location', '/tomac-cove/')
            self.end_headers()
```
WITH:
```
        elif self.path == '/tomac' or self.path == '/tomac/':
            # Legacy route — kept as 301 for one release per PLAN E1.6.
            # Routes to consolidated /deals/ view (sourcing + pipeline).
            # Remove this elif in the release after this one.
            self.send_response(301)
            self.send_header('Location', '/deals/')
            self.end_headers()
```

## Patch 5 — `/tomac/data.json` legacy data endpoint (lines 1747-1752)

KEEP for one release (purely additive read-side compatibility — used
by the pre-rename React bundle's `fetch('/tomac/data.json')` calls).
NO CHANGE this release. Add a TODO comment for the next:

REPLACE (line 1747):
```
        elif self.path == '/tomac/data.json':
```
WITH:
```
        # TODO(E1, next release): remove /tomac/data.json — use /deals/data.json.
        elif self.path == '/tomac/data.json':
```

ADD a NEW handler immediately above it (between line 1740 and 1741):
```
        elif self.path == '/deals/data.json':
            # Canonical data endpoint — same payload as /tomac/data.json.
            if TOMAC_DATA.exists():
                self._serve_file(TOMAC_DATA, 'application/json')
            else:
                self.send_json(404, {'error': 'deal-pipeline-data.json not yet generated'})
```

## Patch 6 — rename `TOMAC_DATA` constant (line 673)

REPLACE (line 673):
```
TOMAC_DATA          = _ROOT / 'data' / 'compiled' / 'deal-pipeline-data.json'  # legacy /tomac/data.json endpoint
```
WITH:
```
DEAL_PIPELINE_DATA  = _ROOT / 'data' / 'compiled' / 'deal-pipeline-data.json'
# Back-compat alias — remove next release after callers migrate.
TOMAC_DATA          = DEAL_PIPELINE_DATA
```

## Patch 7 — `/tomac-cove/` route (line 1847) — DEFER

Per RUN2 brief: "Do NOT touch ~/dashboards/app/templates/*.html — the
search-and-replace tomac→deals is part of HTML P2 strip morning swap."
The `/tomac-cove/` URL is the live portfolio view (different concept
from `/deals/` sourcing funnel). Leave untouched in this patch set.
Logged for the morning HTML swap to consider as a separate decision.

## Patch 8 — allowed_prefixes for tile RBAC (line 1644)

REPLACE (line 1644):
```
            allowed_prefixes.update(['/deals', '/deals/', '/data', '/tomac/data.json'])
```
WITH:
```
            allowed_prefixes.update(['/deals', '/deals/', '/data',
                                     '/deals/data.json', '/tomac/data.json'])
```

---

## Summary

| Patch | Lines | Type      | Risk |
|-------|-------|-----------|------|
| 1     | 379-380 | constant rename + path resolver  | low  |
| 2     | 399-412 | function rename + alias           | low  |
| 3     | 911-926 | JS injection key rename + alias   | low  |
| 4     | 1741-1746 | 301 target change /tomac → /deals/ | medium (user-visible URL change) |
| 5     | 1740/1747 | new /deals/data.json + TODO    | low  |
| 6     | 673   | constant rename + alias              | low  |
| 7     | 1847  | DEFERRED (HTML morning swap)         | n/a  |
| 8     | 1644  | RBAC allow-list addition             | low  |

Apply order: 1 → 2 → 6 → 3 → 5 → 8 → 4 (most-isolated first; the
user-visible 301 change last so it can be reverted independently).
