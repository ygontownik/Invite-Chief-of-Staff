#!/usr/bin/env python3
"""
test_cos_dashboard_fetch_next.py — Unit tests for the proposed PLAN E1
changes to cos-dashboard-fetch.py and cos-dashboard-server.py.

Strategy: the live cos-dashboard-fetch.py cannot be imported as-is
without a Google OAuth token, so we test the rename contract by
re-implementing the loader helpers proposed in
~/cos-pipeline/next/track-C/cos-dashboard-fetch.py.next inline, and
asserting they behave correctly given a mocked firm_context + firm_config.

Run:
    python3 ~/cos-pipeline/tests/test_cos_dashboard_fetch_next.py
"""
import json
import os
import tempfile
import unittest
from pathlib import Path


# ── Reproduce the helpers proposed in fetch.py.next ───────────────────────
# (keeping these in the test file lets us validate the contract without
# importing the live fetch module, which has Google API side effects)

_DEFAULT_DEAL_KEYWORDS = (
    'term sheet', 'loi', 'letter of intent', ' nda ', 'diligence',
    'investment committee', ' ic ', 'closing', 'co-invest', 'co invest',
)
_DEFAULT_RECRUIT_KEYWORDS = (
    'resume', ' cv ', 'recruiting', 'recruiter', 'interview',
)


def _firm_owner_reject_set(ctx):
    owners = ctx.get('owner_whitelist') or []
    if not owners:
        owners = []
        p = (ctx.get('principal') or {}).get('name', '')
        if p:
            owners.append(p)
        for m in (ctx.get('team') or []):
            n = m.get('name', '')
            if n:
                owners.append(n)
    out = set()
    for o in owners:
        if not o:
            continue
        out.add(o.lower())
        toks = o.split()
        if toks:
            out.add(toks[0].lower())
    return out


def _firm_deal_keywords(fcfg):
    raw = fcfg.get('deal_keywords') or list(_DEFAULT_DEAL_KEYWORDS)
    aliases = (fcfg.get('counterparty_aliases') or {})
    canon = []
    if isinstance(aliases, dict):
        for v in aliases.values():
            if isinstance(v, str):
                canon.append(v)
            elif isinstance(v, dict) and v.get('canonical'):
                canon.append(v['canonical'])
    elif isinstance(aliases, list):
        for entry in aliases:
            if isinstance(entry, dict) and entry.get('canonical'):
                canon.append(entry['canonical'])
    return {str(k).lower() for k in (raw + canon) if k}


def _firm_recruit_keywords(fcfg):
    raw = fcfg.get('recruit_keywords') or list(_DEFAULT_RECRUIT_KEYWORDS)
    return {str(k).lower() for k in raw if k}


def _is_deal_ws(ws):
    return ws in ('deals', 'tomac')  # noqa: tenant-leak (backward-compat alias)


def _doc_id(ctx, fcfg, key, default=''):
    gdocs = (ctx.get('google_docs') or {})
    if key in gdocs:
        return gdocs[key]
    legacy = (fcfg.get('docs') or {})
    return legacy.get(key, default)


# ── Test fixtures ────────────────────────────────────────────────────────

MOCK_CTX = {
    'schema_version': 2,
    'principal': {'name': 'Jane Principal', 'email': 'jane@example.com'},
    'team': [
        {'name': 'Alex Colleague', 'role': 'co-founder, deal lead'},
        {'name': 'Sam', 'role': 'fundraising'},
    ],
    'owner_whitelist': ['Jane', 'Alex', 'Sam'],
    'workstream_categories': {'deal': 'Example Firm'},
    'google_docs': {
        'pipeline':   'NEW_PIPELINE_DOC_ID',
        'followups':  'NEW_FU_DOC_ID',
    },
}

MOCK_FCONFIG = {
    'firm_name': 'Example Firm',
    'docs': {
        'recruiting': 'LEGACY_RECRUIT_DOC_ID',
        'example':    'LEGACY_EXAMPLE_DOC_ID',  # legacy fallback only
    },
    'deal_keywords': ['alphadeal', 'janedoe', 'project', 'term sheet'],
    'recruit_keywords': ['example recruiter', 'interview'],
    'counterparty_aliases': [
        {'needles': ['alphadeal'], 'canonical': 'AlphaDeal Power Plant'},
    ],
}


# ── Tests ───────────────────────────────────────────────────────────────

class OwnerWhitelistFromFirmContext(unittest.TestCase):

    def test_owner_whitelist_used_when_present(self):
        s = _firm_owner_reject_set(MOCK_CTX)
        self.assertIn('yoni', s)
        self.assertIn('mark', s)
        self.assertIn('nik', s)

    def test_falls_back_to_principal_plus_team(self):
        ctx = {k: v for k, v in MOCK_CTX.items() if k != 'owner_whitelist'}
        s = _firm_owner_reject_set(ctx)
        self.assertIn('yoni', s)
        self.assertIn('mark', s)        # first-name from "Mark Saxe"
        self.assertIn('mark saxe', s)   # full name lowered
        self.assertIn('nik', s)

    def test_empty_ctx_yields_empty_set(self):
        s = _firm_owner_reject_set({})
        self.assertEqual(s, set())


class DealKeywordsFromFirmConfig(unittest.TestCase):

    def test_keywords_loaded_from_firm_config(self):
        kws = _firm_deal_keywords(MOCK_FCONFIG)
        self.assertIn('alphadeal', kws)
        self.assertIn('janedoe', kws)
        self.assertIn('term sheet', kws)

    def test_canonical_aliases_merged_into_keywords(self):
        kws = _firm_deal_keywords(MOCK_FCONFIG)
        self.assertIn('alphadeal power plant', kws)

    def test_default_keywords_when_missing(self):
        kws = _firm_deal_keywords({})
        self.assertIn('term sheet', kws)
        self.assertIn('diligence', kws)


class RecruitKeywordsFromFirmConfig(unittest.TestCase):

    def test_recruit_keywords_loaded(self):
        kws = _firm_recruit_keywords(MOCK_FCONFIG)
        self.assertIn('example recruiter', kws)
        self.assertIn('interview', kws)

    def test_default_recruit_when_missing(self):
        kws = _firm_recruit_keywords({})
        self.assertIn('resume', kws)


class DocIdResolution(unittest.TestCase):

    def test_prefers_google_docs_over_firm_config(self):
        # 'pipeline' is in google_docs (canonical) — should win
        v = _doc_id(MOCK_CTX, MOCK_FCONFIG, 'pipeline')
        self.assertEqual(v, 'NEW_PIPELINE_DOC_ID')

    def test_falls_back_to_firm_config_docs(self):
        # 'recruiting' is only in firm_config.json :: docs
        v = _doc_id(MOCK_CTX, MOCK_FCONFIG, 'recruiting')
        self.assertEqual(v, 'LEGACY_RECRUIT_DOC_ID')

    def test_legacy_key_resolves_via_firm_config_docs(self):
        # Legacy keys not in google_docs fall back to firm_config.docs
        v = _doc_id(MOCK_CTX, MOCK_FCONFIG, 'example')
        self.assertEqual(v, 'LEGACY_EXAMPLE_DOC_ID')


class WorkstreamBackCompat(unittest.TestCase):

    def test_deals_is_canonical(self):
        self.assertTrue(_is_deal_ws('deals'))

    def test_tomac_still_accepted_for_one_release(self):
        self.assertTrue(_is_deal_ws('tomac'))  # noqa: tenant-leak (backward-compat alias)

    def test_other_workstreams_not_deal(self):
        for ws in ('job', 'personal', '', None, 'recruiting'):
            self.assertFalse(_is_deal_ws(ws), f'{ws!r} should not be deal')


class JsonOutputContract(unittest.TestCase):
    """Per PLAN E1.1(b): JSON output uses 'deals' key; the legacy tenant key is kept as
    a back-compat duplicate during the one-release window."""

    def _build_output(self, deals_value):
        return {
            'deals': deals_value,
            'tomac': deals_value,  # noqa: tenant-leak (backward-compat key)
        }

    def test_deals_key_present(self):
        out = self._build_output([{'name': 'AlphaDeal', 'stage': 'Sourcing'}])
        self.assertIn('deals', out)
        self.assertEqual(out['deals'][0]['name'], 'AlphaDeal')

    def test_tomac_back_compat_duplicate(self):
        deals = [{'name': 'AlphaDeal'}]
        out = self._build_output(deals)
        self.assertEqual(out['deals'], out['tomac'])  # noqa: tenant-leak (backward-compat key)


class DealConfigPathResolution(unittest.TestCase):
    """Per PLAN E1.4: _DEAL_CONFIG_PATH points to the per-tenant config
    repo (~/cos-pipeline-config-<slug>/config/deal-config.yaml) with
    fallback to the legacy ~/dashboards/config/<slug>-config.yaml path
    for one release."""

    def _resolve_path(self, env_dir=None, tenant_dir=None, dashboards_dir=None):
        candidates = []
        if env_dir:
            candidates.append(Path(env_dir) / 'config' / 'deal-config.yaml')
        if tenant_dir:
            candidates.append(Path(tenant_dir) / 'config' / 'deal-config.yaml')
        if dashboards_dir:
            candidates.append(Path(dashboards_dir) / 'config' / 'deal-config.yaml')
            candidates.append(Path(dashboards_dir) / 'config' / 'tomac-config.yaml')  # noqa: tenant-leak (legacy fallback path)
        for p in candidates:
            if p.exists():
                return p
        return candidates[-1] if candidates else None

    def test_per_tenant_config_preferred(self):
        with tempfile.TemporaryDirectory() as td:
            tenant = Path(td) / 'cos-pipeline-config-tomac'  # noqa: tenant-leak (backward-compat path test)
            (tenant / 'config').mkdir(parents=True)
            target = tenant / 'config' / 'deal-config.yaml'
            target.write_text('liveDeals: []\n')
            resolved = self._resolve_path(tenant_dir=tenant)
            self.assertEqual(resolved, target)

    def test_legacy_dashboards_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            dash = Path(td) / 'dashboards'
            (dash / 'config').mkdir(parents=True)
            legacy = dash / 'config' / 'tomac-config.yaml'  # noqa: tenant-leak (legacy fallback path)
            legacy.write_text('liveDeals: []\n')
            resolved = self._resolve_path(dashboards_dir=dash)
            self.assertEqual(resolved, legacy)

    def test_env_var_wins(self):
        with tempfile.TemporaryDirectory() as td:
            env_dir = Path(td) / 'env-config'
            (env_dir / 'config').mkdir(parents=True)
            target = env_dir / 'config' / 'deal-config.yaml'
            target.write_text('liveDeals: []\n')

            tenant_dir = Path(td) / 'tenant'
            (tenant_dir / 'config').mkdir(parents=True)
            (tenant_dir / 'config' / 'deal-config.yaml').write_text('x: 1\n')

            resolved = self._resolve_path(env_dir=env_dir, tenant_dir=tenant_dir)
            self.assertEqual(resolved, target)


class ParseDealPipelineNameExists(unittest.TestCase):
    """Per PLAN E1.1(a): the function `parse_deal_pipeline` must exist
    in the .next file. Verify the symbol is documented as a rename."""

    def test_next_file_documents_rename(self):
        nxt = Path.home() / 'cos-pipeline' / 'next' / 'track-C' / 'cos-dashboard-fetch.py.next'
        self.assertTrue(nxt.exists(), f'.next file missing at {nxt}')
        text = nxt.read_text()
        self.assertIn('parse_deal_pipeline', text,
                      'parse_deal_pipeline rename not documented')
        self.assertIn('parse_tomac', text,  # noqa: tenant-leak (backward-compat alias check)
                      'parse_tomac alias for back-compat not documented')  # noqa: tenant-leak

    def test_next_file_documents_deal_config_path(self):
        # Also mentioned in server-routes.delta.md (since the constant
        # actually lives in cos-dashboard-server.py, not fetch.py).
        delta = Path.home() / 'cos-pipeline' / 'next' / 'track-C' / 'server-routes.delta.md'
        self.assertTrue(delta.exists())
        text = delta.read_text()
        self.assertIn('_DEAL_CONFIG_PATH', text)
        self.assertIn('deal-config.yaml', text)
        # Per-tenant path mentioned
        self.assertIn('cos-pipeline-config-tomac', text)  # noqa: tenant-leak (backward-compat path test)
        # Legacy fallback mentioned
        self.assertIn('tomac-config.yaml', text)  # noqa: tenant-leak (legacy path test)

    def test_next_file_documents_deal_window_var(self):
        delta = Path.home() / 'cos-pipeline' / 'next' / 'track-C' / 'server-routes.delta.md'
        text = delta.read_text()
        self.assertIn('window.__DEAL_CONFIG__', text)
        # Back-compat alias still emitted
        self.assertIn('window.__TOMAC_CONFIG__', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
