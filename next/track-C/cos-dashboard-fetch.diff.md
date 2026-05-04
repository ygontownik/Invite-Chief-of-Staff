# cos-dashboard-fetch.diff.md — annotated diff: live → .next (PLAN E1)

Live source: `~/cos-pipeline/cos-dashboard-fetch.py` (2,151 lines).
Proposal:    `~/cos-pipeline/next/track-C/cos-dashboard-fetch.py.next`
             (delta-instructions; full rewrite was rejected because the
             same identifier 'tomac' is used as 4 distinct concepts —
             workstream code, doc-id key, JSON output key, function name —
             across 40 sites, and an automated rewrite would silently
             cross-contaminate them).

Format: REPLACE / WITH blocks with line numbers from the live file.

---

### CHUNK 1 — top-of-file firm context block

REPLACE @ lines 28-40:
```
# Firm context — loads firm_context.yaml for config that varies by team
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _firm_context as _fc
_CTX     = _fc.load_firm_context()
MY_EMAIL = (_fc._principal(_CTX).get('email') or 'ygontownik@gmail.com')

DOC_IDS = {
    'followups':    '10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY',
    'recruiting':   '1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA',
    'tomac':        '1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc',
    'briefing_log': '14wE3L6ZRsjhhx2psRKbaHS5i0kgEoteWYZusqETiAZ0',
    'daily_market': '1UZ1t4bhgzll5VcAuP3Mj1CyYb-4xjgmbUK1xg6oUS_k',
}
```
WITH: see DELTA 1 in `cos-dashboard-fetch.py.next`. Adds `_FCONFIG`
load + `_doc_id()` resolver; key `tomac` → `deal_pipeline` with
`DOC_IDS['tomac']` retained as alias.

### CHUNK 2 — owner whitelist + keyword loaders (NEW INSERTION)

INSERT immediately after the new DOC_IDS block (around line 41):
see DELTA 2 in `cos-dashboard-fetch.py.next` — adds
`_firm_owner_reject_set`, `_firm_deal_keywords`,
`_firm_recruit_keywords`, and module-level `_DEAL_KEYS`,
`_RECRUIT_KEYS`, `_OWNER_REJECT`.

### CHUNK 3 — DELETE hardcoded keyword dicts

DELETE @ lines 113-125 (the `_RECRUIT_KEYS = { ... }` literal).
DELETE @ lines 127-133 (the `_DEAL_KEYS = { ... }` literal).
Replaced by the loader calls in CHUNK 2.

### CHUNK 4 — Gmail query string

REPLACE @ lines 168-179 (the `query = ( 'newer_than:2d (' ... )` block):
see DELTA 3 in `cos-dashboard-fetch.py.next`.

### CHUNK 5 — workstream label map (parse_followups)

REPLACE @ line 386:
```
    ws_map = {'Job Search': 'job', 'Tomac Cove': 'tomac', 'Personal': 'personal'}
```
WITH:
```
    _ws_deal_label = (_CTX.get('workstream_categories') or {}).get('deal', 'Tomac Cove')
    ws_map = {'Job Search': 'job', _ws_deal_label: 'deals', 'Personal': 'personal'}
    ws_map.setdefault('Tomac Cove', 'deals')
```

### CHUNK 6 — function rename

REPLACE @ line 548:
```
def parse_tomac(text):
```
WITH:
```
def parse_deal_pipeline(text):
    """Parse the deal-pipeline doc into deal cards.
    Renamed from parse_tomac in PLAN E1.1. Old name kept as alias."""
```
APPEND immediately after the function body (line 587 area):
```
parse_tomac = parse_deal_pipeline   # back-compat alias (1 release)
```

### CHUNK 7 — skip-set in parse_deal_pipeline

REPLACE @ line 550:
```
    skip = {'Tomac Cove — Deal Pipeline', 'Template'}
```
WITH:
```
    _deal_label = (_CTX.get('workstream_categories') or {}).get('deal', 'Tomac Cove')
    skip = {f'{_deal_label} — Deal Pipeline', 'Template'}
```

### CHUNK 8 — REJECT_NAMES set in _is_deal_shaped_cp

REPLACE @ lines 849-857 (`REJECT_NAMES = { ... }` literal):
```
    REJECT_NAMES = {
        'yoni','mark','nik','nick','jason','guillermo','ansel','sarah','jeff',
        'kevin','tim','dan','david','brian','joey','andrew','mike','john','matt',
        'chris','tom','paul','bob','rob','steve','sam','pete','will','greg',
        'gideon','powell','ian','ryan','max','joe','adam','alex','ben','ed',
        'frank','gary','henry','james','kate','laura','lisa','molly','nate',
        'oscar','peter','rick','scott','ted','victor','walter','mark saxe',
        'tanmay kumar','sydney mcconathy','andrew brannan','sarah graziano',
    }
```
WITH:
```
    REJECT_NAMES = _OWNER_REJECT | {
        'kevin','tim','dan','david','brian','joey','andrew','mike','john','matt',
        'chris','tom','paul','bob','rob','steve','sam','pete','will','greg',
        'gideon','powell','ian','ryan','max','joe','adam','alex','ben','ed',
        'frank','gary','henry','james','kate','laura','lisa','molly','nate',
        'oscar','peter','rick','scott','ted','victor','walter',
    }
```

### CHUNK 9 — _is_deal_ws helper + workstream code reads

ADD a new helper (placed after CHUNK 2 inserts):
```
def _is_deal_ws(ws):
    return ws in ('deals', 'tomac')
```
REPLACE everywhere a literal `'tomac'` workstream-code is checked:
- line 636: `if fu.get('workstream') not in (None, '', 'tomac'):` →
            `if fu.get('workstream') not in (None, '', 'tomac', 'deals'):`
- line 950: `if fu.get('workstream') != 'tomac': continue` →
            `if not _is_deal_ws(fu.get('workstream')): continue`
- lines 1501-1502: ternaries → use `_is_deal_ws(ws)`
- line 1533: workstream literal `'tomac'` → `'deals'`

### CHUNK 10 — calendar classifier (TOMAC_KEYS / JOB_KEYS)

REPLACE @ lines 1659-1675 (the keyword sets + ws ternary):
```
        TOMAC_KEYS = [ ... 14 lines ... ]
        JOB_KEYS = [ ... 4 lines ... ]
        ws = ('tomac' if any(k in hay for k in TOMAC_KEYS)
              else 'job' if any(k in hay for k in JOB_KEYS)
              else 'personal')
```
WITH:
```
        DEAL_KEYS = _DEAL_KEYS
        JOB_KEYS  = _RECRUIT_KEYS
        ws = ('deals' if any(k in hay for k in DEAL_KEYS)
              else 'job' if any(k in hay for k in JOB_KEYS)
              else 'personal')
```

### CHUNK 11 — main() local variable renames

REPLACE @ line 1797:
```
        tom_f = ex.submit(_fetch_doc_worker, DOC_IDS['tomac'],        doc_cache)
```
WITH:
```
        deal_f = ex.submit(_fetch_doc_worker, DOC_IDS['deal_pipeline'], doc_cache)
```

REPLACE @ line 1803:
```
        tomac_text          = tom_f.result()
```
WITH:
```
        deal_pipeline_text  = deal_f.result()
```

REPLACE @ lines 1816-1818:
```
    tomac                = parse_tomac(tomac_text)
    lp_data              = parse_lp_data(tomac_text)
    fundraising_strategy = parse_fundraising_strategy(tomac_text)
```
WITH:
```
    deals                = parse_deal_pipeline(deal_pipeline_text)
    lp_data              = parse_lp_data(deal_pipeline_text)
    fundraising_strategy = parse_fundraising_strategy(deal_pipeline_text)
```

REPLACE @ line 1858 (call to parse_recent_activity):
```
        briefing_data, followups, recruiting, tomac,
```
WITH:
```
        briefing_data, followups, recruiting, deals,
```

REPLACE @ line 1915:
```
        for deal in tomac:
```
WITH:
```
        for deal in deals:
```

REPLACE @ lines 2012-2013:
```
    _tomac_envelope = state.get('awaitingExternal', []) + state.get('dealIntel', []) + state.get('originationInbox', []) + state.get('statusUpdates', [])
    tomac, _promoted = _auto_promote_origination(tomac, followups, _tomac_envelope, today_str, lp_names=lp_data)
```
WITH:
```
    _deals_envelope = state.get('awaitingExternal', []) + state.get('dealIntel', []) + state.get('originationInbox', []) + state.get('statusUpdates', [])
    deals, _promoted = _auto_promote_origination(deals, followups, _deals_envelope, today_str, lp_names=lp_data)
```

REPLACE @ line 2015:
```
        print(f'cos-dashboard-fetch: auto-promoted {len(_promoted)} origination→tomac: ...
```
WITH:
```
        print(f'cos-dashboard-fetch: auto-promoted {len(_promoted)} origination→deals: ...
```

REPLACE @ line 2025:
```
    tomac = _overlay_freshest_signal(tomac, followups, _tomac_envelope, today_str)
```
WITH:
```
    deals = _overlay_freshest_signal(deals, followups, _deals_envelope, today_str)
```

### CHUNK 12 — JSON output

REPLACE @ line 2044 (in `section_timestamps`):
```
        'tomac':            _ts('tomac',            tomac),
```
WITH:
```
        'deals':            _ts('deals',            deals),
        'tomac':            _ts('deals',            deals),   # back-compat (1 release)
```

REPLACE @ line 2065 (in `live_data`):
```
        'tomac':            tomac,
```
WITH:
```
        'deals':            deals,
        'tomac':            deals,                            # back-compat (1 release)
```

---

## Total surface

12 chunks. ~85 line edits. Touches 0 unrelated functions. Three
back-compat aliases written into the .next so a downstream caller
that still references `parse_tomac`, `DOC_IDS['tomac']`, or the
JSON `tomac` key keeps working for one release.

Verification post-application:
```
grep -n "parse_tomac\|DOC_IDS\['tomac'\]\|'tomac':" cos-dashboard-fetch.py
# Expected: 3 lines max (the 3 back-compat alias sites).
```
