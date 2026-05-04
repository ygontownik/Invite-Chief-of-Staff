#!/usr/bin/env python3
"""
_model_router_test.py — standalone tests for _model_router.

Run:
    python3 _model_router_test.py

No pytest dependency; uses unittest. Mocks the anthropic SDK so the
suite runs offline. Writes cost JSONL into a temp dir (overrides
_PIPELINE_ROOT) so it never touches the live data-tomac/costs/ tree.

POST-CUTOVER VERSION (subscription dispatch is wired). Differences vs
the prior live test file:
  - TestSubscriptionDispatch no longer asserts NotImplementedError;
    instead it mocks claude_agent_sdk and verifies the dispatch hands
    off to it. Detailed contract tests live in
    tests/test_model_router_subscription.py.
  - Everything else (TestRouting, TestDaemonDispatch, TestApiDispatch,
    TestQuotas, TestCostMath) is unchanged from the prior version.
"""
from __future__ import annotations

import importlib
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
sys.path.insert(0, str(HERE))


# ─────────────────────────────────────────────────────────────────────
# Helpers — fake Anthropic SDK.
# ─────────────────────────────────────────────────────────────────────

class _FakeUsage(dict):
    pass


class _FakeBlock(dict):
    pass


class _FakeResponse(dict):
    pass


class _FakeMessages:
    def __init__(self, captured: list[dict],
                 in_tok: int = 1000, out_tok: int = 200,
                 cached: int = 0) -> None:
        self.captured = captured
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.cached = cached

    def create(self, **kwargs):
        self.captured.append(kwargs)
        return _FakeResponse(
            content=[_FakeBlock(text="ok")],
            usage=_FakeUsage(
                input_tokens=self.in_tok,
                output_tokens=self.out_tok,
                cache_read_input_tokens=self.cached,
            ),
        )


class _FakeAnthropicClient:
    def __init__(self, captured: list[dict], **usage_kw) -> None:
        self.messages = _FakeMessages(captured, **usage_kw)


def _install_fake_anthropic(captured: list[dict], **usage_kw):
    fake_mod = mock.MagicMock()
    fake_mod.Anthropic = lambda *a, **kw: _FakeAnthropicClient(captured, **usage_kw)
    sys.modules["anthropic"] = fake_mod
    return fake_mod


# ─────────────────────────────────────────────────────────────────────
# Helpers — fake claude_agent_sdk (post-cutover subscription path).
# ─────────────────────────────────────────────────────────────────────

class _FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [types.SimpleNamespace(text=text)]


class _FakeResultMessage:
    def __init__(self, usage: dict) -> None:
        self.usage = usage


class _FakeRateLimitEvent:
    def __init__(self, status: str, resets_at) -> None:
        self.rate_limit_info = types.SimpleNamespace(
            status=status, resets_at=resets_at,
        )


class _FakeProcessError(Exception):
    pass


class _FakeClaudeSDKError(Exception):
    pass


_FakeAssistantMessage.__name__ = "AssistantMessage"
_FakeResultMessage.__name__ = "ResultMessage"
_FakeRateLimitEvent.__name__ = "RateLimitEvent"


def _install_fake_sdk(*, chunks=None, error=None):
    chunks = chunks or []

    async def _gen(prompt=None, options=None):
        if error is not None:
            raise error
        for c in chunks:
            yield c

    def _query(prompt=None, options=None):
        return _gen(prompt=prompt, options=options)

    class _OptionsCapture:
        last_kwargs: dict = {}

        def __init__(self, **kwargs):
            type(self).last_kwargs = dict(kwargs)
            for k, v in kwargs.items():
                setattr(self, k, v)

    fake = types.SimpleNamespace(
        query=_query,
        ClaudeAgentOptions=_OptionsCapture,
        RateLimitEvent=_FakeRateLimitEvent,
        ProcessError=_FakeProcessError,
        ClaudeSDKError=_FakeClaudeSDKError,
    )
    sys.modules["claude_agent_sdk"] = fake
    return fake


# ─────────────────────────────────────────────────────────────────────
# Tests.
# ─────────────────────────────────────────────────────────────────────

class _RouterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Reload module fresh for each test so we can monkey-patch the
        # _PIPELINE_ROOT and _ROUTINES_YAML constants cleanly.
        if "_model_router" in sys.modules:
            del sys.modules["_model_router"]
        self.mr = importlib.import_module("_model_router")

        # Redirect cost output into temp dir.
        self.tenant = "testtenant"
        self.cost_root = Path(self.tmp.name) / "cos-pipeline"
        self.mr._PIPELINE_ROOT = self.cost_root  # type: ignore[attr-defined]

        # Redirect _costs_path to use our root.
        original_costs_path = self.mr._costs_path

        def _patched_costs_path(tenant: str, day=None):
            day = day or datetime.now(timezone.utc)
            p = self.cost_root / f"data-{tenant}" / "costs" / f"{day.strftime('%Y-%m-%d')}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        self.mr._costs_path = _patched_costs_path  # type: ignore[attr-defined]

        # Redirect _data_dir (subscription-mode dispatch ledger + queue).
        if hasattr(self.mr, "_data_dir"):
            def _patched_data_dir(tenant: str):
                p = self.cost_root / f"data-{tenant}"
                p.mkdir(parents=True, exist_ok=True)
                return p
            self.mr._data_dir = _patched_data_dir  # type: ignore[attr-defined]

        self.addCleanup(lambda: sys.modules.pop("claude_agent_sdk", None))


class TestRouting(_RouterTestBase):
    def test_per_pass_defaults(self):
        r = self.mr.resolve_route("pass2_pipeline_analyst", tenant=self.tenant)
        self.assertEqual(r.model, self.mr.MODEL_OPUS_4_7)
        self.assertEqual(r.max_tokens, 4096)
        self.assertEqual(r.source, "claudemd_per_pass")

    def test_pass3_uses_sonnet_4096(self):
        r = self.mr.resolve_route("pass3_ic_memo", tenant=self.tenant)
        self.assertEqual(r.model, self.mr.MODEL_SONNET_4_6)
        self.assertEqual(r.max_tokens, 4096)

    def test_routines_yaml_routine_name(self):
        # `cos-personal-briefing` is in routines.yaml as briefing/subscription.
        r = self.mr.resolve_route("cos-personal-briefing", tenant=self.tenant)
        self.assertEqual(r.package, "briefing")
        self.assertEqual(r.mode, "subscription")

    def test_renamed_target_resolves(self):
        r = self.mr.resolve_route("briefing-morning", tenant=self.tenant)
        self.assertEqual(r.package, "briefing")

    def test_unknown_task_falls_back(self):
        r = self.mr.resolve_route("nonsense-task", tenant=self.tenant)
        self.assertEqual(r.source, "overall_default")
        self.assertEqual(r.model, self.mr.MODEL_SONNET_4_6)

    def test_daemon_package_routes_to_daemon_mode(self):
        # `usage-report` (daemon) has package=infra, mode=api in routines.
        # But a server-package routine should mark daemon.
        r = self.mr.resolve_route("cosdashboard", tenant=self.tenant)
        self.assertEqual(r.mode, "daemon")


class TestSubscriptionDispatch(_RouterTestBase):
    def test_subscription_dispatches_via_sdk(self):
        # Cutover-day test: subscription mode should hand off to
        # claude_agent_sdk.query() and return the assistant text plus
        # subscription_meta. NotImplementedError is gone.
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("ok"),
            _FakeResultMessage({"input_tokens": 1, "output_tokens": 1}),
        ])
        out = self.mr.call_claude(
            "cos-personal-briefing",
            system="firm context",
            messages=[{"role": "user", "content": "hi"}],
            tenant=self.tenant,
        )
        self.assertEqual(out["text"], "ok")
        self.assertEqual(out["est_usd"], 0.0)
        self.assertIn("subscription_meta", out)

    def test_subscription_returns_zero_usd(self):
        # Subscription mode is window-billed, not per-token.
        _install_fake_sdk(chunks=[
            _FakeAssistantMessage("hi"),
            _FakeResultMessage({"input_tokens": 100, "output_tokens": 20}),
        ])
        out = self.mr.call_claude(
            "briefing-morning",
            system=None,
            messages=[{"role": "user", "content": "x"}],
            mode="subscription",
            tenant=self.tenant,
        )
        self.assertEqual(out["est_usd"], 0.0)


class TestDaemonDispatch(_RouterTestBase):
    def test_daemon_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.mr.call_claude(
                "cosdashboard",
                system="x",
                messages=[{"role": "user", "content": "x"}],
                tenant=self.tenant,
            )


class TestApiDispatch(_RouterTestBase):
    def test_api_dispatch_builds_messages_and_cache(self):
        captured: list[dict] = []
        _install_fake_anthropic(captured, in_tok=1000, out_tok=200, cached=400)

        result = self.mr.call_claude(
            "pass2_pipeline_analyst",
            system="FIRM CONTEXT PREAMBLE",
            messages=[{"role": "user", "content": "analyze"}],
            mode="auto",
            cache=True,
            tenant=self.tenant,
        )

        self.assertEqual(len(captured), 1)
        kw = captured[0]
        self.assertEqual(kw["model"], self.mr.MODEL_OPUS_4_7)
        self.assertEqual(kw["max_tokens"], 4096)

        # cache_control must be attached.
        sysblocks = kw["system"]
        self.assertIsInstance(sysblocks, list)
        self.assertEqual(sysblocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(sysblocks[0]["text"], "FIRM CONTEXT PREAMBLE")

        # Result + cost record.
        self.assertEqual(result["text"], "ok")
        self.assertGreater(result["est_usd"], 0)

        # JSONL row written.
        path = self.mr._costs_path(self.tenant)
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "pass2_pipeline_analyst")
        self.assertEqual(rows[0]["model"], self.mr.MODEL_OPUS_4_7)
        self.assertEqual(rows[0]["cached_input_tokens"], 400)
        self.assertEqual(rows[0]["mode"], "api")

    def test_no_cache_when_disabled(self):
        captured: list[dict] = []
        _install_fake_anthropic(captured)
        self.mr.call_claude(
            "pass1_source_scanner",
            system="ctx",
            messages=[{"role": "user", "content": "x"}],
            cache=False,
            mode="api",
            tenant=self.tenant,
        )
        # system should be the raw string, not a block list.
        self.assertEqual(captured[0]["system"], "ctx")


class TestQuotas(_RouterTestBase):
    def _seed_spend(self, task_type: str, est_usd: float) -> None:
        path = self.mr._costs_path(self.tenant)
        with path.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "task_type": task_type,
                "model": self.mr.MODEL_OPUS_4_7,
                "input_tokens": 0, "output_tokens": 0,
                "cached_input_tokens": 0,
                "mode": "api", "est_usd": est_usd,
            }) + "\n")

    def test_warn_at_soft_cap(self):
        # Default cap = 5.00 USD; seed 5.50 to trip warn but not hard-stop.
        self._seed_spend("pass1_source_scanner", 5.50)

        captured: list[dict] = []
        _install_fake_anthropic(captured)

        buf = []
        with mock.patch("sys.stderr") as err:
            err.write = lambda s: buf.append(s)
            self.mr.call_claude(
                "pass1_source_scanner",
                system="x",
                messages=[{"role": "user", "content": "x"}],
                mode="api",
                tenant=self.tenant,
            )
        joined = "".join(buf)
        self.assertIn("WARN", joined)
        self.assertIn("pass1_source_scanner", joined)

    def test_hard_stop_at_3x(self):
        # 3 * 5.00 = 15.00 hard stop.
        self._seed_spend("pass1_source_scanner", 16.00)
        captured: list[dict] = []
        _install_fake_anthropic(captured)
        with self.assertRaises(self.mr.QuotaExceeded):
            self.mr.call_claude(
                "pass1_source_scanner",
                system="x",
                messages=[{"role": "user", "content": "x"}],
                mode="api",
                tenant=self.tenant,
            )
        # No SDK call made.
        self.assertEqual(len(captured), 0)


class TestCostMath(_RouterTestBase):
    def test_estimate_includes_cache_discount(self):
        # Sonnet: $3/M in, $15/M out. 1000 input (400 cached) + 200 out.
        # fresh_in = 600 -> 600/1e6 * 3 = 0.0018
        # cached   = 400 -> 400/1e6 * 3 * 0.10 = 0.00012
        # out      = 200 -> 200/1e6 * 15 = 0.003
        # total ≈ 0.00492
        est = self.mr._estimate_cost(self.mr.MODEL_SONNET_4_6, 1000, 200, 400)
        self.assertAlmostEqual(est, 0.00492, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
