"""Tests for the two watchdog checks whose logic decides whether #alerts gets woken.

Vera's panel flagged their absence on qaEngineer#45 (minor/tests, confirmed): both
scripts document their core functions as "deliberately pure so the rule is testable",
and then shipped no tests. The rule IS the load-bearing part — an attribution bug in
`fallback_requests` means either a silent degrade nobody hears about or a pager that
cries wolf, and neither shows up in a smoke run against a healthy container.

Stdlib unittest on purpose: CI here is python3 with PyYAML and nothing else, and a
watchdog's test suite earning a dependency install is the wrong trade.

    python3 -m unittest discover tests -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_model_fallback import fallback_requests  # noqa: E402
from check_oauth_health import evaluate  # noqa: E402

VERA_IP = "10.0.14.6"
AGENT_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"


def sample(**labels) -> str:
    """One `litellm_proxy_total_requests_metric_total` line, in the gateway's real shape."""
    value = labels.pop("value", 1.0)
    base = {
        "api_key_alias": "studio-gw-75bce515",
        "client_ip": VERA_IP,
        "requested_model": "protolabs/smart",
        "route": "/v1/chat/completions",
        "status_code": "200",
        "user_agent": AGENT_UA,
    }
    base.update(labels)
    rendered = ",".join(f'{k}="{v}"' for k, v in base.items())
    return f"litellm_proxy_total_requests_metric_total{{{rendered}}} {value}"


class FallbackAttribution(unittest.TestCase):
    """The rule: a protoAgent-UA chat completion from Vera's IP IS a fallback."""

    def test_counts_agent_traffic_from_vera(self):
        total, by_model = fallback_requests(sample(value=7.0), VERA_IP)
        self.assertEqual(total, 7.0)
        self.assertEqual(by_model, {"protolabs/smart": 7.0})

    def test_ignores_clawpatch(self):
        # clawpatch shares the gateway key, the container AND the model alias — the
        # user_agent is the only thing separating it from a real fallback. Miscounting
        # it would report a permanent degrade on a perfectly healthy lane.
        total, _ = fallback_requests(sample(user_agent="node", value=94.0), VERA_IP)
        self.assertEqual(total, 0.0)

    def test_ignores_other_containers(self):
        # Fleet peers share the gateway and the key; only the IP tells them apart.
        total, _ = fallback_requests(sample(client_ip="10.0.14.14", value=171.0), VERA_IP)
        self.assertEqual(total, 0.0)

    def test_ignores_embeddings(self):
        # Same UA, same container, not a model-lane fallback.
        line = sample(route="/v1/embeddings", requested_model="qwen3-embedding", value=45.0)
        total, _ = fallback_requests(line, VERA_IP)
        self.assertEqual(total, 0.0)

    def test_sums_across_models_and_skips_noise(self):
        text = "\n".join(
            [
                "# HELP litellm_proxy_total_requests_metric_total noise",
                sample(value=3.0),
                sample(requested_model="protolabs/cloud", value=2.0),
                sample(user_agent="node", value=99.0),
                "litellm_something_else_total{foo=\"bar\"} 5.0",
            ]
        )
        total, by_model = fallback_requests(text, VERA_IP)
        self.assertEqual(total, 5.0)
        self.assertEqual(by_model, {"protolabs/smart": 3.0, "protolabs/cloud": 2.0})

    def test_empty_scrape_is_zero_not_an_error(self):
        # A gateway that just restarted serves no samples yet; that is "nothing to
        # report", not an alarm.
        self.assertEqual(fallback_requests("", VERA_IP), (0.0, {}))


def oauth_status(**over) -> list[dict]:
    base = {
        "provider": "anthropic-oauth",
        "signed_in": True,
        "refreshable": True,
        "source": "instance_store",
        "expires_at": time.time() + 3600,
    }
    base.update(over)
    return [base]


class OAuthHealth(unittest.TestCase):
    NATIVE = {"provider": "anthropic-oauth", "name": "claude-sonnet-5"}

    def test_healthy_lane_passes(self):
        code, lines = evaluate(self.NATIVE, oauth_status())
        self.assertEqual(code, 0)
        self.assertTrue(any("claude-sonnet-5" in line for line in lines))

    def test_signed_out_fails(self):
        code, lines = evaluate(self.NATIVE, oauth_status(signed_in=False, detail="disconnected"))
        self.assertEqual(code, 1)
        self.assertIn("NOT SIGNED IN", lines[-1])

    def test_unrefreshable_credential_fails(self):
        # The CLAUDE_CODE_OAUTH_TOKEN trap: reads signed_in right up until it 401s.
        code, lines = evaluate(self.NATIVE, oauth_status(refreshable=False, source="env"))
        self.assertEqual(code, 1)
        self.assertIn("not refreshable", lines[-1].lower())

    def test_incoherent_provider_and_name_fails(self):
        # protoAgent#2623: one decision, two fields. A gateway alias under a native
        # provider is rejected on every call — silently fatal, and exactly what a
        # half-edited model config produces.
        code, lines = evaluate({"provider": "anthropic-oauth", "name": "protolabs/smart"}, oauth_status())
        self.assertEqual(code, 1)
        self.assertIn("gateway alias", lines[-1])

    def test_missing_status_entry_fails(self):
        code, lines = evaluate(self.NATIVE, [])
        self.assertEqual(code, 1)
        self.assertIn("reports nothing", lines[-1])

    def test_gateway_backed_agent_is_a_noop(self):
        # No subscription, no credential to check — must pass, and say why.
        code, lines = evaluate({"provider": "openai", "name": "protolabs/cloud"}, [])
        self.assertEqual(code, 0)
        self.assertIn("not a native OAuth lane", lines[0])

    def test_messages_carry_no_verdict_prefix(self):
        # main() prepends "OK: "/"FAIL: " from the exit code. A branch that bakes its
        # own prefix in prints "OK: OK: …" — caught in review, and invisible to the
        # other tests here because they all call evaluate() directly and never main().
        for cfg, status in (
            ({"provider": "openai", "name": "protolabs/cloud"}, []),
            (self.NATIVE, oauth_status()),
            (self.NATIVE, oauth_status(signed_in=False)),
        ):
            _, lines = evaluate(cfg, status)
            self.assertFalse(
                lines[0].startswith(("OK:", "FAIL:")),
                f"evaluate() must not prefix its own verdict: {lines[0]!r}",
            )

    def test_expired_but_refreshable_is_not_an_alarm(self):
        # Refresh is ON USE, so a busy agent legitimately sits at or past its access
        # token's expiry. Alarming on proximity would cry wolf every few hours.
        code, _ = evaluate(self.NATIVE, oauth_status(expires_at=time.time() - 60))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
