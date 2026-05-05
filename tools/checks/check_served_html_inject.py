"""check_served_html_inject.py — verify the script-injection block fires.

WHAT IT CATCHES
---------------
The dashboard server injects a <script> block into every served page that
sets ~7 window globals: __DEAL_CONFIG__, __TOMAC_CONFIG__, __DELETIONS__,
__FIRM_CONTEXT__, __TOPICS_INITIAL__, __PERSONAL_ITEMS_INITIAL__,
__RECRUIT_CONFIG__, __USER_ROLE__. The JS render code reads those globals
to populate Team Actions, Fundraising, tombstones, recruit pipeline, etc.

In May 2026 a regression in the injection guard (`'window.__DELETIONS__'
not in html` matched template *consumer* references, not just the setter
form, so the inject was silently skipped on every render). Visible result:
fundraising panel + team actions blank, tombstones inactive, deletes
didn't stick — but the underlying data files were all healthy. The bug
took ~6 round-trips to diagnose because no runtime check verified the
served HTML contained the setters.

This check closes that gap: hit `http://localhost:7777/` over plain HTTP
(no Anthropic/Drive/Gmail API — purely local), parse the response body,
assert every required setter signature is present.

Status:
  pass  : every required setter present
  fail  : any required setter missing (the inject is broken)
  warn  : server unreachable (likely down — log as warn, not fail)

NO API CREDENTIALS REQUIRED. Pure HTTP localhost check.
"""

from __future__ import annotations

import urllib.request
import urllib.error
from typing import Any

# Default port — matches the LaunchAgent for the maintainer install.
# Subscribers running on a different port can override with env var
# COS_DASHBOARD_PORT.
import os
DASHBOARD_URL = f'http://localhost:{os.environ.get("COS_DASHBOARD_PORT", "7777")}/'

# Required setter SIGNATURES — these strings only appear in the injection
# script's emitted output, never in the template body itself. Each must
# be present in the served HTML for the page to render correctly.
REQUIRED_SETTERS = [
    'window.__DEAL_CONFIG__ =',
    'window.__TOMAC_CONFIG__ =',           # back-compat alias setter
    'window.__DELETIONS__ = new Set',       # unique fingerprint vs consumer ref
    'window.__FIRM_CONTEXT__ =',
    'window.__TOPICS_INITIAL__ =',
    'window.__PERSONAL_ITEMS_INITIAL__ =',
    'window.__RECRUIT_CONFIG__ =',
    'window.__USER_ROLE__ =',
]


def run() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=5) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return {
            'name': 'served HTML script injection',
            'rule_ref': 'cos-dashboard-server.py :: _deletions_script() inject guard',
            'status': 'warn',
            'summary': f'server unreachable at {DASHBOARD_URL}: {exc}',
            'details': [
                f'tried: GET {DASHBOARD_URL}',
                'is the dashboard LaunchAgent loaded? '
                'launchctl list | grep cosdashboard',
            ],
        }
    except Exception as exc:
        return {
            'name': 'served HTML script injection',
            'rule_ref': 'cos-dashboard-server.py :: _deletions_script() inject guard',
            'status': 'warn',
            'summary': f'unexpected error fetching dashboard: {exc}',
            'details': [str(exc)],
        }

    missing = [sig for sig in REQUIRED_SETTERS if sig not in html]
    if not missing:
        return {
            'name': 'served HTML script injection',
            'rule_ref': 'cos-dashboard-server.py :: _deletions_script() inject guard',
            'status': 'pass',
            'summary': (
                f'{len(REQUIRED_SETTERS)} required setters all present in '
                f'served HTML ({len(html):,} bytes)'
            ),
            'details': [],
        }

    return {
        'name': 'served HTML script injection',
        'rule_ref': 'cos-dashboard-server.py :: _deletions_script() inject guard',
        'status': 'fail',
        'summary': (
            f'{len(missing)} of {len(REQUIRED_SETTERS)} required window-global '
            f'setters MISSING from served HTML — JS render will use empty stubs '
            f'(fundraising / team-actions / tombstones broken)'
        ),
        'details': [
            f'missing: {sig}' for sig in missing
        ] + [
            f'served HTML size: {len(html):,} bytes',
            f'inspect served HTML: curl -s {DASHBOARD_URL} | grep -E "window\\.__"',
            'guard logic at cos-dashboard-server.py :: do_GET → '
            "_serve_html_template path; check `'window.__DELETIONS__ = new Set' "
            "not in html` triggers the inject.",
        ],
    }
