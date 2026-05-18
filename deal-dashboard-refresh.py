#!/usr/bin/env python3
"""
deal-dashboard-refresh.py
Reads deal-pipeline-data.json, injects it into Deal Pipeline Dashboard.html,
replacing the DATA block between `const DATA = ` and `// __END_DATA__`.

Also merges originationInbox[] from dashboard-data.json so the Theme Discovery
tab in the deal dashboard shows unmatched signals and each theme card shows its
inline Market Signals panel.

Run directly:  python3 ~/dashboards/app/deal-dashboard-refresh.py
Called by:     cos-dashboard-server.py POST /refresh-deals
"""
import json, os, re, sys
from pathlib import Path

# ── Classification flag ────────────────────────────────────────────────────────
# When True, Claude Haiku classifies ALL origination items against ALL live
# themes. Fully dynamic — new themes in deal-pipeline-data.json are evaluated
# automatically at the next refresh, no code change required.
# Set False to fall back to fast keyword matching (no API call, noisier).
CLASSIFY_SIGNALS = os.environ.get('SKIP_HAIKU_CLASSIFY') != '1'

# ── Path → theme mapping ───────────────────────────────────────────────────────
# Used only when CLASSIFY_SIGNALS=False. Maps dashboard_path substrings
# (case-insensitive, structured field) to known theme IDs.
# New themes handled by Haiku when CLASSIFY_SIGNALS=True — no entry needed here.
PATH_THEME_MAP = [
    ('miso',            'miso-power'),
    ('pjm',             'pjm-power'),
    ('wecc',            'wecc-power'),
    ('epc',             'epc-rollup'),
    ('fsru',            'eu-lng-fsru'),
    ('european lng',    'eu-lng-fsru'),
    ('us lng',          'us-lng-pf'),
    ('lng export',      'us-lng-pf'),
    ('lng project',     'us-lng-pf'),
    ('dc queue',        'dc-queue'),
    ('distressed land', 'dc-queue'),
    ('captive power',   'captive-power'),
    ('der-grid',        'der-grid'),
]


def _extract_theme_name_tokens(theme: dict) -> list[str]:
    """Return matchable tokens derived from the theme's own name and thesis.
    Used as dynamic fallback (CLASSIFY_SIGNALS=False) so new themes work."""
    tokens = []
    name = theme.get('theme', '')
    words = [w.strip('()') for w in name.split() if len(w.strip('()')) >= 5]
    tokens.extend(words)
    if len(words) >= 2:
        tokens.append(' '.join(words[:2]))
    thesis = theme.get('thesis', '')
    proper = re.findall(r'\b[A-Z][A-Za-z]{3,}(?:\s+[A-Z][A-Za-z]{3,})*\b', thesis)
    tokens.extend(proper[:12])
    return list(dict.fromkeys(tokens))


def tag_origination_items(items: list, theme_ids_list: list, themes: list = None) -> None:
    """Keyword-only fallback classifier (CLASSIFY_SIGNALS=False).

    Pass 1: dashboard_path substring match (PATH_THEME_MAP, structured field).
    Pass 2: dynamic tokens from live theme name + thesis proper nouns.
    Items with no match get theme_ids=[].
    """
    themes = themes or []
    theme_map = {t.get('id'): t for t in themes}

    for item in items:
        matched = set()
        text_blob = ' '.join(filter(None, [
            item.get('content', ''),
            item.get('context', ''),
            item.get('counterparty', ''),
        ]))
        path = (item.get('dashboard_path') or '').lower()

        # Pass 1 — dashboard_path substring (reliable; path is structured)
        for substr, tid in PATH_THEME_MAP:
            if tid in theme_ids_list and substr in path:
                matched.add(tid)

        # Pass 2 — dynamic tokens from theme name/thesis (handles any theme)
        for tid in theme_ids_list:
            if tid in matched:
                continue
            theme = theme_map.get(tid, {})
            for token in _extract_theme_name_tokens(theme):
                if len(token) >= 5 and token in text_blob:
                    matched.add(tid)
                    break

        item['theme_ids'] = sorted(matched)


def _classify_all_items_haiku(items: list, theme_map: dict) -> int:
    """Classify ALL origination items against all live themes using Haiku.

    Fully dynamic: theme descriptions are read from the live theme_map so any
    theme added to deal-pipeline-data.json is automatically evaluated at the
    next refresh — no code change needed.

    Items are batched 10 per Haiku call to minimise API round-trips.
    Writes confirmed theme_ids[] on each item in-place.
    Returns count of items matched to at least one theme.
    """
    # Route through _claude_dispatch so subscription mode is honored.
    _HERE_DDR = Path(__file__).resolve().parent
    if str(_HERE_DDR) not in sys.path:
        sys.path.insert(0, str(_HERE_DDR))
    try:
        import _claude_dispatch  # noqa: PLC0415
    except ImportError:
        print('  WARNING: _claude_dispatch not on path — skipping Haiku classification')
        return 0

    import json as _json

    # Build theme descriptions once from live data
    theme_lines = []
    for tid, theme in theme_map.items():
        thesis = (theme.get('thesis') or '')[:250]
        theme_lines.append(f'{tid}: {theme.get("theme", tid)}\n  {thesis}')
    theme_block = '\n'.join(theme_lines)

    BATCH_SIZE = 10
    confirmed = 0

    for batch_start in range(0, len(items), BATCH_SIZE):
        batch = items[batch_start:batch_start + BATCH_SIZE]
        item_sections = []
        for item in batch:
            item_sections.append(
                f'ID:{item.get("id", "")}\n'
                f'Party:{item.get("counterparty", "")}\n'
                f'Content:{(item.get("content") or "")[:300]}\n'
                f'Context:{(item.get("context") or "")[:150]}'
            )
        items_block = '\n---\n'.join(item_sections)

        prompt = (
            'Infrastructure investment analyst task: classify origination items against themes.\n\n'
            f'THEMES (id: name, thesis):\n{theme_block}\n\n'
            f'ITEMS:\n{items_block}\n\n'
            'For each item that is a GENUINE signal for a theme — a specific deal, company '
            'development, or market event directly relevant to the theme thesis (not a '
            'passing mention) — include it in the output.\n'
            'Reply ONLY with JSON: {"<item-id>":["<theme-id>",...],...}\n'
            'Omit items with no genuine matches. Use exact IDs as given.'
        )

        try:
            raw = _claude_dispatch.call(
                task_type='deal_dashboard_refresh_classify',
                model='claude-haiku-4-5-20251001',
                max_tokens=512,
                messages=[{'role': 'user', 'content': prompt}],
                cache=False,
            ).strip()
            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]
            verdicts = _json.loads(raw.strip())
            valid_tids = set(theme_map.keys())
            for item in batch:
                item_id = item.get('id', '')
                matched = [t for t in verdicts.get(item_id, []) if t in valid_tids]
                item['theme_ids'] = sorted(matched)
                if matched:
                    confirmed += 1
        except Exception as e:
            print(f'  WARNING: Haiku batch [{batch_start}:{batch_start+BATCH_SIZE}] failed — {e}')
            for item in batch:
                if 'theme_ids' not in item:
                    item['theme_ids'] = []

    return confirmed


_HERE         = Path(__file__).parent                       # ~/dashboards/app/ (preserve symlink — see cos-dashboard-refresh.py)
_ROOT         = _HERE.parent                                 # ~/dashboards/
JSON_PATH     = _ROOT / 'data' / 'compiled' / 'deal-pipeline-data.json'
COS_DATA_PATH = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'
# HTML strip P2 paths (Track 1.7) — see cos-dashboard-refresh.py for full notes.
HTML_TEMPLATE = _HERE / 'templates' / 'deal-dashboard.template.html'
HTML_RENDERED = _HERE / 'templates' / 'deal-dashboard.rendered.html'
HTML_PATH     = _HERE / 'templates' / 'deal-dashboard.html'


def assemble_data():
    """Load deal-pipeline JSON, merge origination inbox, classify themes.

    Extracted from main() as part of the HTML strip P2 refactor (see
    HTML_STRIP_RUNBOOK.md). Lets the template/rendered split helper
    reuse the same data-assembly logic without a subprocess hop.

    Returns the full data dict ready for HTML injection.
    """
    # ── Read deal pipeline JSON ────────────────────────────
    if not JSON_PATH.exists():
        print(f'ERROR: JSON not found: {JSON_PATH}', file=sys.stderr)
        sys.exit(1)
    try:
        with open(JSON_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'ERROR reading JSON: {e}', file=sys.stderr)
        sys.exit(1)

    # ── Merge originationInbox from dashboard-data.json ────
    if COS_DATA_PATH.exists():
        try:
            with open(COS_DATA_PATH, encoding='utf-8') as f:
                cos = json.load(f)
            data['originationInbox'] = cos.get('originationInbox', [])
            print(f'  Merged {len(data["originationInbox"])} origination items from dashboard-data.json')
        except Exception as e:
            print(f'  WARNING: could not read dashboard-data.json ({e}) — originationInbox will be empty')
            data['originationInbox'] = []
    else:
        print(f'  WARNING: dashboard-data.json not found — originationInbox will be empty')
        data['originationInbox'] = []

    # ── Tag origination items with matching theme IDs ──────
    themes_list = data.get('themes', [])
    theme_ids   = [t.get('id', '') for t in themes_list]
    theme_map   = {t.get('id'): t for t in themes_list}

    if CLASSIFY_SIGNALS and data['originationInbox']:
        # Dynamic path: Haiku evaluates ALL items against ALL live themes.
        # New themes added to deal-pipeline-data.json are picked up automatically.
        for item in data['originationInbox']:
            item['theme_ids'] = []
        confirmed = _classify_all_items_haiku(data['originationInbox'], theme_map)
        print(f'  Haiku: {confirmed}/{len(data["originationInbox"])} items matched to themes')
    else:
        # Fast fallback: path + dynamic token matching, no API call
        tag_origination_items(data['originationInbox'], theme_ids, themes=themes_list)
        tagged = sum(1 for i in data['originationInbox'] if i.get('theme_ids'))
        print(f'  Keyword mode: {tagged}/{len(data["originationInbox"])} items tagged')

    return data


def main():
    data = assemble_data()
    compact_json = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    # ── Read HTML ──────────────────────────────────────────
    # Read from clean .template.html when present; fall back to legacy .html
    # (bootstrap case before the template was generated).
    if HTML_TEMPLATE.exists():
        source_path = HTML_TEMPLATE
    elif HTML_PATH.exists():
        source_path = HTML_PATH
    else:
        print(f'ERROR: neither template nor legacy HTML found '
              f'({HTML_TEMPLATE} / {HTML_PATH})', file=sys.stderr)
        sys.exit(1)
    html = source_path.read_text(encoding='utf-8')

    # ── Replace DATA block ─────────────────────────────────
    # Use a callable replacement so backslash sequences in compact_json
    # (e.g. JSON-escaped \n, \t, \\) aren't reinterpreted by re.sub. A
    # string-form replacement would turn JSON's \n back into a literal
    # newline byte and break the inline const DATA = {...} parse.
    pattern = r'(const DATA = ).*?(; // __END_DATA__)'
    new_html, n = re.subn(
        pattern,
        lambda m: m.group(1) + compact_json + '; // __END_DATA__',
        html,
        count=1,
        flags=re.DOTALL,
    )

    if n == 0:
        print('ERROR: DATA block not found in HTML — marker missing', file=sys.stderr)
        sys.exit(1)

    # ── Write back ─────────────────────────────────────────
    # Write to .rendered.html (new server-read path) AND mirror to legacy
    # .html for rollback safety during transition.
    HTML_RENDERED.write_text(new_html, encoding='utf-8')
    HTML_PATH.write_text(new_html, encoding='utf-8')

    theme_count  = len(data.get('themes', []))
    target_count = sum(len(th.get('targets', [])) for th in data.get('themes', []))
    orig_count   = len(data.get('originationInbox', []))
    week         = data.get('week_number', '?')
    print(f'Deal Pipeline Dashboard refreshed — week {week}, {theme_count} themes, '
          f'{target_count} targets, {orig_count} origination items '
          f'→ {HTML_RENDERED.name} (+ legacy {HTML_PATH.name})')


if __name__ == '__main__':
    main()
