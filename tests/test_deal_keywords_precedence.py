"""test_deal_keywords_precedence.py — pin the deal_keywords precedence chain.

Per session-4 robustness pass: load_config() in cos_gmail_mini_v2.py resolves
deal_keywords + recruit_keywords through this chain:
  1. firm_config.json :: deal_keywords (per-tenant override — wins)
  2. ~/cos-pipeline/domains/<domain>/config.yaml :: deal_keywords (middle)
  3. DEFAULT_CONFIG hardcoded (last resort — tomac asset names like 'cholla')

Without #2 a real-estate tenant would fall through to tomac asset names for
email triage classification, mislabeling RE deals as non-deals.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import cos_gmail_mini_v2 as gmm


class LoadDomainBundle(unittest.TestCase):

    def test_returns_empty_when_no_domain_in_ctx(self):
        self.assertEqual(gmm._load_domain_bundle({}), {})
        self.assertEqual(gmm._load_domain_bundle({"firm": "x"}), {})

    def test_returns_empty_when_bundle_dir_missing(self):
        # Domain that doesn't exist on disk.
        self.assertEqual(
            gmm._load_domain_bundle({"domain": "no-such-domain-xyz"}),
            {},
        )

    def test_loads_real_estate_bundle(self):
        cfg = gmm._load_domain_bundle({"domain": "real-estate"})
        self.assertIn("deal_keywords", cfg,
                      msg="real-estate bundle should expose deal_keywords")
        # Real-estate-specific keywords should be present
        self.assertIn("cap rate", cfg["deal_keywords"])
        self.assertIn("NOI", cfg["deal_keywords"])
        # Tomac-specific keywords should NOT be in the real-estate bundle
        self.assertNotIn("cholla", cfg["deal_keywords"])

    def test_loads_infra_pe_bundle(self):
        cfg = gmm._load_domain_bundle({"domain": "infra-pe"})
        self.assertIn("deal_keywords", cfg)


class DealKeywordsPrecedence(unittest.TestCase):
    """The full chain: tenant override → domain bundle → hardcoded default."""

    def _build_config(self, user_config, ctx):
        """Replicate the keyword-resolution branch of load_config()."""
        domain_cfg = gmm._load_domain_bundle(ctx)
        result = {}
        for f in ("deal_keywords", "recruit_keywords"):
            if f in user_config:
                result[f] = user_config[f]
            elif f in domain_cfg:
                result[f] = domain_cfg[f]
            else:
                result[f] = gmm.DEFAULT_CONFIG[f]
        return result

    def test_tenant_override_wins(self):
        # Tenant explicitly sets deal_keywords — that wins, ignoring domain + default.
        cfg = self._build_config(
            user_config={"deal_keywords": ["my-custom-term"]},
            ctx={"domain": "real-estate"},
        )
        self.assertEqual(cfg["deal_keywords"], ["my-custom-term"])

    def test_domain_bundle_wins_over_default(self):
        # No tenant override but domain set — domain bundle wins over hardcoded.
        cfg = self._build_config(
            user_config={},
            ctx={"domain": "real-estate"},
        )
        # Should be real-estate terms, NOT tomac defaults
        self.assertIn("cap rate", cfg["deal_keywords"])
        self.assertNotIn("cholla", cfg["deal_keywords"])

    def test_default_used_when_no_domain_and_no_override(self):
        # No tenant override and no domain — falls through to hardcoded defaults.
        cfg = self._build_config(
            user_config={},
            ctx={},
        )
        # Tomac hardcoded terms
        self.assertIn("cholla", cfg["deal_keywords"])

    def test_default_used_when_domain_bundle_missing_field(self):
        # Domain bundle exists but lacks a particular keyword field.
        # generic-dealmaker bundle: check what it has and use a missing one.
        domain_cfg = gmm._load_domain_bundle({"domain": "generic-dealmaker"})
        # Force a scenario where domain has deal_keywords but not recruit_keywords
        with mock.patch.object(gmm, "_load_domain_bundle",
                               return_value={"deal_keywords": ["x"]}):
            cfg = self._build_config(
                user_config={},
                ctx={"domain": "generic-dealmaker"},
            )
            # deal_keywords should be from domain
            self.assertEqual(cfg["deal_keywords"], ["x"])
            # recruit_keywords should fall through to default
            self.assertEqual(cfg["recruit_keywords"], gmm.DEFAULT_CONFIG["recruit_keywords"])


if __name__ == "__main__":
    unittest.main()
