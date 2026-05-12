"""test_model_router_subscription.py — tests for the subscription
dispatch path written into ``_model_router.py.next``.

Why an independent test file?
- The live ``_model_router.py`` still raises ``NotImplementedError`` for
  ``mode='subscription'``. Tonight's draft sits beside it as
  ``_model_router.py.next``. These tests load the draft module by file
  path (the ``.next`` suffix isn't on Python's import path), mock the
  ``claude_agent_sdk`` chunk generator, and assert the production
  contract documented in ``CSPIKE_PLAN.md`` and the prompt for Run 6.

Run:
    python3 tests/test_model_router_subscription.py

Stays offline — never imports the real claude_agent_sdk.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────
# Module loader for the .next draft.
# ─────────────────────────────────────────────────────────────────────

NEXT_PATH = ROOT / "_model_router.py.next"
LIVE_PATH = ROOT / "_model_router.py"


def _load_next_module() -> types.ModuleType:
    """Load the subscription-dispatch module fresh per test.

    Pre-cutover: loads ``_model_router.py.next`` (this file's draft).
    Post-cutover: ``.next`` will be gone — fall back to the live
    ``_model_router.py``. Same test file works both sides of cutover.

    Reload from disk avoids state leakage between tests (TIME_*_TASKS
    sets are immutable, but _PIPELINE_ROOT gets monkey-patched per test).
    """
    from importlib.machinery import SourceFileLoader
    import importlib.util

    target = NEXT_PATH if NEXT_PATH.exists() else LIVE_PATH

    name = "_mr_under_test"
    sys.modules.pop(name, None)
    loader = SourceFileLoader(name, str(target))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ─────────────────────────────────────────────────────────────────────
# Fake claude_agent_sdk for the dispatch path to import.
# ─────────────────────────────────────────────────────────────────────

class _FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        block = types.SimpleNamespace(text=text)
        self.content = [block]


class _FakeRateLimitEvent:
    def __init__(self, status: str, resets_at):
        self.rate_limit_info = types.SimpleNamespace(
            status=status, resets_at=resets_at,
        )


class _FakeResultMessage:
    def __init__(self, usage: dict) -> None:
        self.usage = usage


class _FakeProcessError(Exception):
    pass


class _FakeClaudeSDKError(Exception):
    pass


class _FakeOptionsCapture:
    """Drop-in for ClaudeAgentOptions that records kwargs for assertion."""
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)
        for k, v in kwargs.items():
            setattr(self, k, v)


def _install_fake_sdk(*, chunks=None, error=None):
    """Install a fake claude_agent_sdk in sys.modules.

    chunks: list of objects (FakeAssistantMessage etc.) yielded in order.
    error:  if set, raised inside the async generator before any chunk.
    """
    chunks = chunks or []

    async def _gen(prompt=None, options=None):
        if error is not None:
            raise error
        for c in chunks:
            yield c

    def _query(prompt=None, options=None):
        return _gen(prompt=prompt, options=options)

    fake = types.SimpleNamespace(
        query=_query,
        ClaudeAgentOptions=_FakeOptionsCapture,
        RateLimitEvent=_FakeRateLimitEvent,
        ProcessError=_FakeProcessError,
        ClaudeSDKError=_FakeClaudeSDKError,
    )
    sys.modules["claude_agent_sdk"] = fake
    return fake


def _uninstall_fake_sdk():
    sys.modules.pop("claude_agent_sdk", None)
    _FakeOptionsCapture.last_kwargs = {}


# Type-name shim — _run_subscription_query inspects type(chunk).__name__,
# so make sure our fakes report the names production code expects.
_FakeAssistantMessage.__name__ = "AssistantMessage"
_FakeRateLimitEvent.__name__ = "RateLimitEvent"
_FakeResultMessage.__name__ = "ResultMessage"


# ─────────────────────────────────────────────────────────────────────
# Test base — load .next module + redirect _PIPELINE_ROOT to tmp.
# ─────────────────────────────────────────────────────────────────────

class _SubBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tenant = "testtenant"

        self.mr = _load_next_module()

        # Redirect data + cost output into temp dir.
        self.root = Path(self.tmp.name) / "cos-pipeline"
        self.mr._PIPELINE_ROOT = self.root  # type: ignore[attr-defined]

        original_costs_path = self.mr._costs_path

        def _patched_costs_path(tenant: str, day=None):
            day = day or datetime.now(timezone.utc)
            p = self.root / f"data-{tenant}" / "costs" / f"{day.strftime('%Y-%m-%d')}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        def _patched_data_dir(tenant: str):
            p = self.root / f"data-{tenant}"
            p.mkdir(parents=True, exist_ok=True)
            return p

        self.mr._costs_path = _patched_costs_path  # type: ignore[attr-defined]
        self.mr._data_dir = _patched_data_dir       # type: ignore[attr-defined]

        # Tests should never load the real SDK; install a no-op default
        # that errors if reached without a per-test override.
        self.addCleanup(_uninstall_fake_sdk)


# ─────────────────────────────────────────────────────────────────────
# Tests.
# ─────────────────────────────────────────────────────────────────────

class TestPromptBuilding(_SubBase):
    def test_string_system_inlined_first(self):
        out = self.mr._build_subscription_prompt(
            "FIRM CONTEXT",
            [{"role": "user", "content": "do thing"}],
        )
        self.assertTrue(out.startswith("FIRM CONTEXT"))
        self.assertIn("[user]", out)
        self.assertIn("do thing", out)

    def test_block_system_flattens_text(self):
        out = self.mr._build_subscription_prompt(
            [{"type": "text", "text": "PREAMBLE"}],
            [{"role": "user", "content": "x"}],
        )
        self.assertTrue(out.startswith("PREAMBLE"))

    def test_assistant_role_marker_preserved(self):
        out = self.mr._build_subscription_prompt(
            None,
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "ack"}],
        )
        self.assertIn("[user]", out)
        self.assertIn("[assistant]", out)
        self.assertIn("ack", out)

    def test_none_system_omitted(self):
        out = self.mr._build_subscription_prompt(
            None,
            [{"role": "user", "content": "hi"}],
        )
        self.assertEqual(out, "[user]\nhi")


class TestMinimalOptions(_SubBase):
    """Cost-story test: bare-mode options must reach ClaudeAgentOptions."""

    def test_minimal_options_applied(self):
        result_msg = _FakeResultMessage({"input_tokens": 10, "output_tokens": 5})
        ai = _FakeAssistantMessage("pong")
        _install_fake_sdk(chunks=[ai, result_msg])

        route = self.mr.ModelRoute(
            task_type="cos-personal-briefing",
            model=self.mr.MODEL_SONNET_4_6,
            mode="subscription",
            max_tokens=2048,
            package="briefing",
        )
        out = self.mr._dispatch_subscription(
            route=route, system="ctx",
            messages=[{"role": "user", "content": "go"}],
            cache=True, tenant=self.tenant,
        )
        kw = _FakeOptionsCapture.last_kwargs
        # Cost-locked fields per CSPIKE_PLAN.md path-comparison matrix.
        self.assertEqual(kw["model"], self.mr.MODEL_SONNET_4_6)
        self.assertEqual(kw["tools"], [])
        self.assertIsNone(kw["skills"])
        self.assertEqual(kw["mcp_servers"], {})
        self.assertEqual(kw["setting_sources"], [])
        self.assertEqual(kw["plugins"], [])
        self.assertIsNone(kw["system_prompt"])
        # Sanity: text accumulated.
        self.assertEqual(out["text"], "pong")
        # Subscription mode = $0 marginal.
        self.assertEqual(out["est_usd"], 0.0)


class TestModelHonored(_SubBase):
    def test_route_model_flows_into_options(self):
        _install_fake_sdk(chunks=[_FakeResultMessage({})])
        route = self.mr.ModelRoute(
            task_type="tomac-deal-compile",  # noqa: tenant-leak (task_type key test)
            model=self.mr.MODEL_OPUS_4_7,
            mode="subscription",
            max_tokens=4096,
            package="deals",
        )
        self.mr._dispatch_subscription(
            route=route, system=None,
            messages=[{"role": "user", "content": "x"}],
            cache=False, tenant=self.tenant,
        )
        self.assertEqual(
            _FakeOptionsCapture.last_kwargs["model"],
            self.mr.MODEL_OPUS_4_7,
        )


class TestRateLimitCapture(_SubBase):
    def test_latest_status_wins(self):
        chunks = [
            _FakeRateLimitEvent("allowed", 1700000000),
            _FakeAssistantMessage("ok"),
            _FakeRateLimitEvent("exceeded", 1700001234),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ]
        _install_fake_sdk(chunks=chunks)
        route = self.mr.ModelRoute(
            task_type="cos-personal-briefing",
            model=self.mr.MODEL_SONNET_4_6,
            mode="subscription",
            max_tokens=2048,
            package="briefing",
        )
        out = self.mr._dispatch_subscription(
            route=route, system=None,
            messages=[{"role": "user", "content": "x"}],
            cache=False, tenant=self.tenant,
        )
        meta = out["subscription_meta"]
        self.assertEqual(meta["rate_limit_status"], "exceeded")
        self.assertEqual(meta["rate_limit_resets_at"], 1700001234)


class TestDispatchLedger(_SubBase):
    def test_successful_call_writes_dispatch_jsonl(self):
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("hi"),
            _FakeResultMessage({"input_tokens": 100, "output_tokens": 20}),
        ])
        route = self.mr.ModelRoute(
            task_type="briefing-morning",
            model=self.mr.MODEL_SONNET_4_6,
            mode="subscription",
            max_tokens=2048,
            package="briefing",
        )
        self.mr._dispatch_subscription(
            route=route, system=None,
            messages=[{"role": "user", "content": "x"}],
            cache=False, tenant=self.tenant,
        )
        ledger = self.root / f"data-{self.tenant}" / "dispatch.jsonl"
        self.assertTrue(ledger.exists())
        rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Required schema fields.
        for k in ("ts", "task_type", "model", "mode", "outcome",
                  "usage", "rate_limit_status"):
            self.assertIn(k, row)
        self.assertEqual(row["task_type"], "briefing-morning")
        self.assertEqual(row["mode"], "subscription")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["model"], self.mr.MODEL_SONNET_4_6)


class TestRateLimitFallback(_SubBase):
    def _route(self, task_type: str) -> "ModelRoute":  # type: ignore[name-defined]
        return self.mr.ModelRoute(
            task_type=task_type,
            model=self.mr.MODEL_SONNET_4_6,
            mode="subscription",
            max_tokens=2048,
            package="briefing",
        )

    def test_time_insensitive_queues_on_process_error(self):
        # ProcessError -> queue (TIME_INSENSITIVE branch).
        # We hit it by feeding the chunk-loop a RateLimitEvent first
        # (so resets_at is captured) then raising ProcessError.
        # Simpler: install_fake_sdk with error= raises immediately —
        # no rate-limit context, but task is still TIME_INSENSITIVE,
        # so it queues with queue_until=None.
        sdk = _install_fake_sdk(error=_FakeProcessError("rate_limit_exceeded"))
        route = self._route("cos-personal-briefing")
        out = self.mr._dispatch_subscription(
            route=route, system=None,
            messages=[{"role": "user", "content": "x"}],
            cache=False, tenant=self.tenant,
        )
        self.assertTrue(out.get("queued"))
        # Queue file should exist with one row.
        q = self.root / f"data-{self.tenant}" / "queue.jsonl"
        self.assertTrue(q.exists())
        rows = [json.loads(l) for l in q.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "cos-personal-briefing")
        self.assertEqual(rows[0]["error_type"], "ProcessError")
        # Dispatch ledger also captures the failure outcome.
        ledger = self.root / f"data-{self.tenant}" / "dispatch.jsonl"
        ledger_rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        self.assertEqual(len(ledger_rows), 1)
        self.assertTrue(ledger_rows[0]["outcome"].startswith("failure:"))

    def test_time_sensitive_reraises_on_process_error(self):
        _install_fake_sdk(error=_FakeProcessError("rate_limit_exceeded"))
        route = self._route("cos-otter-transcripts")
        with self.assertRaises(_FakeProcessError):
            self.mr._dispatch_subscription(
                route=route, system=None,
                messages=[{"role": "user", "content": "x"}],
                cache=False, tenant=self.tenant,
            )
        # No queue file written (re-raise path).
        q = self.root / f"data-{self.tenant}" / "queue.jsonl"
        self.assertFalse(q.exists())
        # Dispatch ledger still captures the failure for observability.
        ledger = self.root / f"data-{self.tenant}" / "dispatch.jsonl"
        self.assertTrue(ledger.exists())

    def test_unknown_task_reraises(self):
        # Task in neither set -> conservative: re-raise.
        _install_fake_sdk(error=_FakeClaudeSDKError("auth lost"))
        route = self._route("totally-unrecognized-task-xyz")
        with self.assertRaises(_FakeClaudeSDKError):
            self.mr._dispatch_subscription(
                route=route, system=None,
                messages=[{"role": "user", "content": "x"}],
                cache=False, tenant=self.tenant,
            )


class TestCallClaudeIntegration(_SubBase):
    """Top-level entry point should route subscription -> dispatch helper."""

    def test_call_claude_subscription_routes_to_dispatch(self):
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("ack"),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ])
        out = self.mr.call_claude(
            "cos-personal-briefing",
            system="ctx",
            messages=[{"role": "user", "content": "x"}],
            mode="subscription",
            tenant=self.tenant,
        )
        self.assertEqual(out["text"], "ack")
        self.assertEqual(out["est_usd"], 0.0)
        self.assertIn("subscription_meta", out)


class TestAuthModeOverride(_SubBase):
    """firm_context.yaml :: auth_mode is a HARD tenant-level override."""

    def test_apply_subscription_override_forces_subscription(self):
        # Routine says api; tenant flips auth_mode=subscription.
        out = self.mr._apply_auth_mode_override("api", "subscription")
        self.assertEqual(out, "subscription")

    def test_apply_api_override_forces_api(self):
        out = self.mr._apply_auth_mode_override("subscription", "api")
        self.assertEqual(out, "api")

    def test_apply_daemon_never_overridden(self):
        # Daemons aren't callable Claude tasks; override must skip them.
        self.assertEqual(
            self.mr._apply_auth_mode_override("daemon", "subscription"),
            "daemon",
        )
        self.assertEqual(
            self.mr._apply_auth_mode_override("daemon", "api"),
            "daemon",
        )

    def test_apply_none_leaves_routine_mode(self):
        # No auth_mode set -> backward compat (routine.mode wins).
        self.assertEqual(
            self.mr._apply_auth_mode_override("subscription", None),
            "subscription",
        )
        self.assertEqual(
            self.mr._apply_auth_mode_override("api", None),
            "api",
        )

    def test_resolve_route_honors_auth_mode_subscription(self):
        # Patch _load_auth_mode to return 'subscription'; then a routine
        # whose own mode is 'api' should resolve to subscription.
        with mock.patch.object(self.mr, "_load_auth_mode",
                               return_value="subscription"):
            r = self.mr.resolve_route("pass1_source_scanner",
                                      tenant=self.tenant)
            self.assertEqual(r.mode, "subscription")

    def test_resolve_route_honors_auth_mode_api(self):
        with mock.patch.object(self.mr, "_load_auth_mode",
                               return_value="api"):
            r = self.mr.resolve_route("cos-personal-briefing",
                                      tenant=self.tenant)
            self.assertEqual(r.mode, "api")

    def test_explicit_mode_arg_beats_auth_mode(self):
        # Caller passes mode='api' explicitly -> auth_mode is ignored.
        with mock.patch.object(self.mr, "_load_auth_mode",
                               return_value="subscription"):
            r = self.mr.resolve_route("pass1_source_scanner",
                                      mode="api", tenant=self.tenant)
            self.assertEqual(r.mode, "api")


class TestClaudeProjectsTelemetry(_SubBase):
    """claude_projects[package] is read at dispatch and recorded in the ledger."""

    def test_project_id_in_subscription_meta_when_set(self):
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("ok"),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ])
        with mock.patch.object(self.mr, "_load_claude_projects",
                               return_value={"briefing": "proj-abc"}):
            route = self.mr.ModelRoute(
                task_type="cos-personal-briefing",
                model=self.mr.MODEL_SONNET_4_6,
                mode="subscription",
                max_tokens=2048,
                package="briefing",
            )
            out = self.mr._dispatch_subscription(
                route=route, system=None,
                messages=[{"role": "user", "content": "x"}],
                cache=False, tenant=self.tenant,
            )
        self.assertEqual(out["subscription_meta"]["project_id"], "proj-abc")

    def test_project_id_null_when_package_unmapped(self):
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("ok"),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ])
        with mock.patch.object(self.mr, "_load_claude_projects",
                               return_value={"deals": "proj-xyz"}):
            route = self.mr.ModelRoute(
                task_type="cos-personal-briefing",
                model=self.mr.MODEL_SONNET_4_6,
                mode="subscription",
                max_tokens=2048,
                package="briefing",
            )
            out = self.mr._dispatch_subscription(
                route=route, system=None,
                messages=[{"role": "user", "content": "x"}],
                cache=False, tenant=self.tenant,
            )
        self.assertIsNone(out["subscription_meta"]["project_id"])

    def test_project_id_recorded_in_dispatch_ledger(self):
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("ok"),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ])
        with mock.patch.object(self.mr, "_load_claude_projects",
                               return_value={"briefing": "proj-ledger"}):
            route = self.mr.ModelRoute(
                task_type="briefing-morning",
                model=self.mr.MODEL_SONNET_4_6,
                mode="subscription",
                max_tokens=2048,
                package="briefing",
            )
            self.mr._dispatch_subscription(
                route=route, system=None,
                messages=[{"role": "user", "content": "x"}],
                cache=False, tenant=self.tenant,
            )
        ledger = self.root / f"data-{self.tenant}" / "dispatch.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_id"], "proj-ledger")

    def test_load_claude_projects_returns_empty_when_no_config(self):
        # No firm_config.json anywhere -> returns {}.
        # The default tenant 'tomac' may have a real firm_config, so
        # use a never-existing tenant slug.
        out = self.mr._load_claude_projects("nonexistent-slug-xyz")
        self.assertEqual(out, {})


class TestStdlibFallback(_SubBase):
    """Pre-cutover safety: if _firm_context.py is the live version (no
    accessors yet), the router's stdlib fallback must fire for
    _load_auth_mode and _load_claude_projects.
    """

    def test_load_auth_mode_falls_back_when_fc_lacks_accessor(self):
        # Inject a fake _firm_context module with NO load_auth_mode.
        fake_fc = types.SimpleNamespace()  # no load_auth_mode attribute
        with mock.patch.dict(sys.modules, {"_firm_context": fake_fc}):
            # And monkey-patch _FIRM_CONTEXT to a tmp YAML containing the field.
            yaml_path = self.root / "firm_context.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("auth_mode: subscription\n")
            with mock.patch.object(self.mr, "_FIRM_CONTEXT", yaml_path):
                self.assertEqual(self.mr._load_auth_mode(), "subscription")

    def test_load_auth_mode_returns_none_when_field_absent(self):
        fake_fc = types.SimpleNamespace()
        with mock.patch.dict(sys.modules, {"_firm_context": fake_fc}):
            yaml_path = self.root / "firm_context.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("schema_version: 2\n")
            with mock.patch.object(self.mr, "_FIRM_CONTEXT", yaml_path):
                self.assertIsNone(self.mr._load_auth_mode())

    def test_load_claude_projects_falls_back_when_fc_lacks_accessor(self):
        # Inject a fake _firm_context module with NO load_claude_projects.
        fake_fc = types.SimpleNamespace()
        # Stage a per-tenant firm_config.json under tmp_root.
        cfg_dir = self.root / "cos-pipeline-config-falltest"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "firm_config.json").write_text(json.dumps({
            "claude_projects": {"briefing": "proj-fallback"}
        }))
        # Point Path.home() at our root so the fallback search hits cfg_dir.
        with mock.patch.dict(sys.modules, {"_firm_context": fake_fc}):
            with mock.patch.object(self.mr, "Path") as patched_path:
                # Path() is used as Path.home() / f"cos-pipeline-config-{tenant}"...
                patched_path.home.return_value = self.root
                # _PIPELINE_ROOT also gets re-derived; keep it pointing at root.
                self.mr._PIPELINE_ROOT = self.root
                out = self.mr._load_claude_projects("falltest")
        self.assertEqual(out, {"briefing": "proj-fallback"})

    def test_load_claude_projects_returns_empty_when_no_config_anywhere(self):
        fake_fc = types.SimpleNamespace()
        with mock.patch.dict(sys.modules, {"_firm_context": fake_fc}):
            # Use a tenant name that won't have a config dir on disk.
            out = self.mr._load_claude_projects("definitely-no-such-tenant-zzz")
        self.assertEqual(out, {})


class TestApiPathStillWorks(_SubBase):
    """Regression: api dispatch must keep working unchanged."""

    def test_api_dispatch_via_fake_anthropic(self):
        captured: list[dict] = []

        class _FakeMsgs:
            def create(self, **kwargs):
                captured.append(kwargs)
                return {
                    "content": [{"text": "ok"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                    },
                }

        class _FakeClient:
            def __init__(self, *a, **kw):
                self.messages = _FakeMsgs()

        fake_anth = mock.MagicMock()
        fake_anth.Anthropic = _FakeClient
        sys.modules["anthropic"] = fake_anth
        self.addCleanup(lambda: sys.modules.pop("anthropic", None))

        result = self.mr.call_claude(
            "pass2_pipeline_analyst",
            system="FIRM CTX",
            messages=[{"role": "user", "content": "analyze"}],
            mode="api",
            cache=True,
            tenant=self.tenant,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["model"], self.mr.MODEL_OPUS_4_7)
        # Cost record written for api mode (cost JSONL, not dispatch.jsonl).
        cost_file = self.mr._costs_path(self.tenant)
        self.assertTrue(cost_file.exists())
        rows = [json.loads(l) for l in cost_file.read_text().splitlines()]
        self.assertEqual(rows[0]["mode"], "api")
        self.assertEqual(result["text"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
