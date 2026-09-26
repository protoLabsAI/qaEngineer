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


class FallbackModelFilter(unittest.TestCase):
    """The lane-shape-independent rule: only traffic to a CONFIGURED fallback counts.

    Added when Vera moved from a native-OAuth primary back to a gateway primary
    (2026-08-23). Under the old "any gateway traffic is a fallback" rule that switch
    silently inverted the check — every ordinary review would have read as a degrade and
    paged #alerts every 15 minutes. These pin the rule that makes it survive a lane
    change without anyone editing the script.
    """

    def test_primary_traffic_is_not_a_fallback(self):
        text = sample(requested_model="protolabs/smart", value=40.0)
        total, _ = fallback_requests(text, VERA_IP, {"protolabs/cloud"})
        self.assertEqual(total, 0.0, "the primary lane doing its job is not a degrade")

    def test_fallback_traffic_counts(self):
        text = sample(requested_model="protolabs/cloud", value=7.0)
        total, by_model = fallback_requests(text, VERA_IP, {"protolabs/cloud"})
        self.assertEqual(total, 7.0)
        self.assertEqual(by_model, {"protolabs/cloud": 7.0})

    def test_mixed_traffic_counts_only_the_fallback(self):
        text = "\n".join([
            sample(requested_model="protolabs/smart", value=100.0),   # primary
            sample(requested_model="protolabs/cloud", value=3.0),     # fallback
            sample(requested_model="protolabs/cloud", user_agent="node", value=50.0),  # clawpatch
        ])
        total, by_model = fallback_requests(text, VERA_IP, {"protolabs/cloud"})
        self.assertEqual(total, 3.0)
        self.assertEqual(by_model, {"protolabs/cloud": 3.0})

    def test_none_counts_every_model(self):
        # The native-primary shape: nothing configured to filter on, so any agent
        # gateway call is a fallback by construction.
        text = sample(requested_model="protolabs/smart", value=9.0)
        total, _ = fallback_requests(text, VERA_IP, None)
        self.assertEqual(total, 9.0)


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



from check_review_health import window_health  # noqa: E402
from smoke_replay import evaluate as smoke_evaluate  # noqa: E402

NOW = 1_790_400_000.0


def reviewed(ts_ago_s, *, complete=True, structural=False, lanes=None, reason=None):
    row = {"event": "reviewed", "ts": NOW - ts_ago_s, "repo": "o/r", "pr": 1, "complete": complete}
    if structural:
        row["structural_unavailable"] = True
        row["structural_reason"] = reason
    if lanes:
        row["incomplete_finders"] = list(lanes)
    return row


class RecentWindow(unittest.TestCase):
    """The rule: judge the last N hours on their own — lifetime averages hide an evening."""

    def test_quiet_window_is_healthy(self):
        rows = [reviewed(60 * i) for i in range(1, 8)]
        problems, summary = window_health(rows, now=NOW, hours=6)
        self.assertEqual(problems, [])
        self.assertIn("reviewed=7 incomplete=0", summary)

    def test_incomplete_rate_needs_a_minimum_sample(self):
        # 2 of 3 incomplete is 67% but three rows is noise — no alarm.
        rows = [reviewed(10, complete=False, lanes=["find_crossfile"]), reviewed(20, complete=False), reviewed(30)]
        self.assertEqual(window_health(rows, now=NOW)[0], [])

    def test_incomplete_rate_alarms_and_names_the_lanes(self):
        rows = [reviewed(10 * i, complete=False, lanes=["find_crossfile"]) for i in range(1, 4)]
        rows += [reviewed(100, complete=False, structural=True, reason="exit-4:gateway-timeout")]
        rows += [reviewed(200), reviewed(300)]
        problems, _ = window_health(rows, now=NOW)
        self.assertEqual(len(problems), 1)  # the rate (4/6) trips; one structural outage is under its own ceiling
        self.assertTrue(any("4/6 rounds incomplete" in p and "find_crossfile×3" in p for p in problems))

    def test_structural_outages_alarm_with_their_classes(self):
        rows = [reviewed(10 * i, complete=False, structural=True, reason="exit-4:gateway-timeout") for i in range(1, 5)]
        rows += [reviewed(500 + i) for i in range(6)]  # 4/10 = 40%, not above the rate ceiling
        problems, _ = window_health(rows, now=NOW)
        self.assertEqual(len(problems), 1)
        self.assertIn("structural lane unavailable on 4 rounds", problems[0])
        self.assertIn("exit-4:gateway-timeout", problems[0])

    def test_exhaustions_and_retries_are_counted_from_their_own_events(self):
        rows = [{"event": "exhaustion", "ts": NOW - 100 * i, "repo": "o/terminal-plugin", "pr": 10} for i in range(3)]
        rows += [{"event": "panel_retry", "ts": NOW - 50 * i, "repo": "o/r", "pr": i, "failed": ["verify"]} for i in range(5)]
        problems, _ = window_health(rows, now=NOW)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("3 exhausted rounds" in p and "terminal-plugin#10" in p for p in problems))
        self.assertTrue(any("5 panel retries" in p and "verify" in p for p in problems))

    def test_rows_outside_the_window_do_not_count(self):
        rows = [reviewed(7 * 3600, complete=False, structural=True) for _ in range(10)]
        self.assertEqual(window_health(rows, now=NOW, hours=6), ([], "window6h: reviewed=0 incomplete=0 structural_out=0 exhaustions=0 retries=0"))


def replay(step_seconds, *, empty_diff=False, failed=(), degraded=(), verdict="PASS", findings=()):
    return {
        "runs": [
            {
                "run": {"repo": "o/r", "pr": 1, "head": "a" * 40},
                "verdict": verdict,
                "findings": list(findings),
                "telemetry": {
                    "failed_steps": list(failed),
                    "degraded_steps": list(degraded),
                    "empty_diff": empty_diff,
                    "step_seconds": step_seconds,
                },
            }
        ]
    }


REAL_PANEL = {
    "find_structural": 137.95,
    "find_conventions": 162.3,
    "find_correctness": 258.05,
    "find_crossfile": 287.89,
    "find_removed_behavior": 366.8,
    "synthesize": 5.15,
    "verify": 5.03,
    "report": 6.18,
}


class SmokeEvaluate(unittest.TestCase):
    """The rule: a replay is believed only when every lane demonstrably ran."""

    def test_the_2026_09_26_post_roll_replay_passes(self):
        problems, summary = smoke_evaluate(replay(REAL_PANEL))
        self.assertEqual(problems, [])
        self.assertIn("verdict=PASS findings=0", summary)
        self.assertIn("structural=138s", summary)

    def test_merged_head_shape_is_refused_on_the_lane_floor(self):
        fast = {**REAL_PANEL, **{s: 5.0 for s in ("find_correctness", "find_crossfile", "find_conventions")}}
        problems, _ = smoke_evaluate(replay(fast))
        self.assertEqual(len(problems), 1)
        self.assertIn("merged-head trap", problems[0])
        self.assertIn("find_correctness=5s", problems[0])

    def test_empty_diff_failed_and_missing_steps_are_each_named(self):
        partial = {k: v for k, v in REAL_PANEL.items() if k != "verify"}
        problems, _ = smoke_evaluate(replay(partial, empty_diff=True, failed=["verify"]))
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("empty_diff" in p for p in problems))
        self.assertTrue(any("failed steps: verify" in p for p in problems))
        self.assertTrue(any("never ran: verify" in p for p in problems))

    def test_a_degraded_lane_is_reported_not_failed(self):
        problems, summary = smoke_evaluate(replay(REAL_PANEL, degraded=["find_crossfile"], verdict="WARN"))
        self.assertEqual(problems, [])
        self.assertIn("degraded=['find_crossfile']", summary)

    def test_no_run_is_a_failure(self):
        problems, summary = smoke_evaluate({"runs": []})
        self.assertEqual(summary, "no run")
        self.assertTrue(problems and problems[0].startswith("no run"))


if __name__ == "__main__":
    unittest.main()
