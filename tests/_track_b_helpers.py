"""Shared helpers for Track B (.next) tests.

Builds a minimal tenant config tree in a temp dir and points $COS_CONFIG_DIR
at it so the .next companion files load deterministically without touching
the real ~/cos-pipeline-config* directories.
"""
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def make_tenant_config(tmp_path: Path) -> Path:
    """Create firm_context.yaml + drive-docs.yaml + firm_config.json under tmp_path."""
    (tmp_path / "firm_context.yaml").write_text(textwrap.dedent("""\
        schema_version: 2
        tenant_slug: testtenant
        principal:
          name: "Test Principal"
          email: "tp@example.com"
          role: "managing director, infrastructure PE"
          background: "12 years"
          investor_frame: "principal investor"
          investment_focus:
            - "power & utilities"
            - "digital infrastructure"
        firm:
          name: "Test Firm Partners"
          short_name: "TFP"
        team:
          - name: "Codealee Lead"
            role: "co-founder, deal lead"
            background: "ex-Apex"
            internal_call_role: "drives the agenda"
        owner_whitelist:
          - "Test"
          - "Codealee"
        workstream_categories:
          deal: "Test Firm Deals"
          recruiting: "Recruiting"
          other: "Other"
        key_people:
          - name: "Codealee Lead"
            context: "Deal lead"
            flag_in_actions: true
        peer_firms: ["Stonepeak", "ECP"]
        counterparty_aliases: []
    """))

    (tmp_path / "drive-docs.yaml").write_text(textwrap.dedent("""\
        docs:
          followups:
            doc_id: TEST_FOLLOWUPS_DOC
            name: Follow-ups
          recruiting:
            doc_id: TEST_RECRUITING_DOC
            name: Recruiting
          tomac_pipeline:
            doc_id: TEST_PIPELINE_DOC
            name: Deal Pipeline
          daily_market_update:
            doc_id: TEST_MARKET_DOC
            name: Daily Market
          briefing_log:
            doc_id: TEST_BRIEFING_DOC
            name: Briefing Log
          people_crm:
            doc_id: TEST_PEOPLE_DOC
            name: People CRM
        folders: {}
    """))

    (tmp_path / "firm_config.json").write_text(json.dumps({
        "firm_name": "Test Firm Partners",
        "keychain_service_prefix": "cos-pipeline-testtenant",
        "docs": {
            "followups":  "TEST_FOLLOWUPS_DOC",
            "pipeline":   "TEST_PIPELINE_DOC",
            "people":     "TEST_PEOPLE_DOC",
            "recruiting": "TEST_RECRUITING_DOC",
        },
        "research_senders": {"example.com": "TEST_RESEARCH_DOC"},
        "podcast_feeds": {"Test Show": "https://example.com/feed.rss"},
    }))

    return tmp_path


def isolated_env(tmp_path: Path):
    """Set COS_CONFIG_DIR + ensure repo on sys.path. Returns prior env for restore."""
    prior = os.environ.get("COS_CONFIG_DIR")
    os.environ["COS_CONFIG_DIR"] = str(tmp_path)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return prior


def restore_env(prior):
    if prior is None:
        os.environ.pop("COS_CONFIG_DIR", None)
    else:
        os.environ["COS_CONFIG_DIR"] = prior


def fresh_import(module_path: Path, module_name: str):
    """Import a .py(.next) file at module_path under module_name."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    sys.modules.pop(module_name, None)
    loader = SourceFileLoader(module_name, str(module_path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    loader.exec_module(mod)
    return mod
