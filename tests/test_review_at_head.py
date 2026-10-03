"""The `Review at head` gate — scripts/review_at_head.py.

This script is the sole required context on `main`, so these are tests of a GUARD: each
case is a way the gate used to pass a head it should not have (qaEngineer#73, ported from
protoLabsAI/protoAgent#3543), plus the incident it exists to catch — a verdict for an older
head must not satisfy the merged one.

Stdlib unittest, like test_watchdog_checks.py: CI here is python3 + PyYAML. protoAgent's
copy of this suite is pytest; the cases below are the same ones, restated.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "review_at_head", Path(__file__).resolve().parents[1] / "scripts" / "review_at_head.py"
)
rah = importlib.util.module_from_spec(_SPEC)
# Register BEFORE exec: `@dataclass` resolves annotations via `sys.modules[cls.__module__]`,
# which is absent for a spec-loaded module and raises.
sys.modules[_SPEC.name] = rah
_SPEC.loader.exec_module(rah)

# Real SHAs from the incident this gate is built for (protoAgent#3298).
REVIEWED = "373d27593952480244cf203392d5a8b5e0ef0087"
MERGED = "7721e5b974c17ae5b7fb4f4a003f9d8fb8103447"


def review(head, verdict="PASS", *, login=rah.REVIEWER_LOGIN, body=None):
    """A review as the GitHub API returns it, carrying the panel's real marker shape."""
    if body is None:
        body = (
            f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted=false -->\n"
            "## QA panel review\n\nsome prose\n"
        )
    return {"user": {"login": login}, "body": body}


class DecideTests(unittest.TestCase):
    def test_a_verdict_for_an_older_head_does_not_satisfy_the_merged_head(self):
        self.assertFalse(rah.decide([review(REVIEWED)], MERGED, []).ok)

    def test_a_verdict_at_the_merged_head_passes(self):
        self.assertTrue(rah.decide([review(MERGED)], MERGED, []).ok)

    def test_WARN_at_head_passes_because_the_qa_tier_is_advisory(self):
        self.assertTrue(rah.decide([review(MERGED, "WARN")], MERGED, []).ok)

    def test_an_explicitly_blocking_verdict_fails(self):
        for verdict in sorted(rah.BLOCKING_VERDICTS):
            with self.subTest(verdict=verdict):
                self.assertFalse(rah.decide([review(MERGED, verdict)], MERGED, []).ok)

    def test_a_marker_with_no_verdict_attribute_fails_closed(self):
        # A marker is not a verdict. It used to read as "?", miss BLOCKING_VERDICTS, and
        # return success — the gate passing a head it never got a verdict for.
        body = f"<!-- protoagent-qa-review head={MERGED} -->\n## QA panel review\n\nsome prose\n"
        decision = rah.decide([review(MERGED, body=body)], MERGED, [])
        self.assertFalse(decision.ok)
        self.assertIn("carries no verdict", decision.description)


HEAD = MERGED
FILE = "src/engagement.rs"
MAJOR = {
    "file": FILE,
    "line": 42,
    "severity": "major",
    "claim": "Unconditional Engaged exclusions silently alter legacy behaviour.",
    "evidence": "if state == State::Engaged { return; }",
    "verdict": "confirmed",
}
REFUTED = {"a": f"{FILE}:42", "d": "refuted", "e": True, "h": True}


def token(record):
    """The panel's `disp=` disposition record: unpadded base64url of compact JSON."""
    raw = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def round_review(verdict, findings=(), *, id, record=None, complete=True, verified=True, promoted=False):
    """A panel round as the plugin posts it: the marker, then the findings record."""
    attrs = f"head={HEAD} verdict={verdict} promoted={'true' if promoted else 'false'}"
    attrs += "" if complete else " complete=false"
    attrs += "" if verified else " verified=false"
    attrs += f" disp={token(record)}" if record else ""
    body = (
        f"<!-- protoagent-qa-review {attrs} -->\n## QA panel review — **{verdict}**\n\n"
        "<details>\n<summary>findings JSON (machine-readable)</summary>\n\n"
        f"```json\n{json.dumps(list(findings), indent=2)}\n```\n</details>"
    )
    return {"user": {"login": rah.REVIEWER_LOGIN}, "body": body, "id": id}


def r1():
    return round_review("PASS", id=101)


def r2():
    return round_review("FAIL", [MAJOR], id=102)


def refuting(of=102, **row):
    return {"of": of, "rows": [{**REFUTED, **row}]}


def _canonical(node):
    """Mirror of pr-reviewer-plugin `scripts/vendor_supersede_rule.py::canonical`."""
    if isinstance(node, list):
        return "[" + ",".join(
            _canonical(n)
            for n in node
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
        ) + "]"
    if isinstance(node, ast.AST):
        fields = [
            f"{name}={_canonical(getattr(node, name, None))}"
            for name in node._fields
            if getattr(node, name, None) not in (None, [])
        ]
        return f"{type(node).__name__}(" + ",".join(fields) + ")"
    return repr(node)


class SupersedeTests(unittest.TestCase):
    """Strictest round per head, except a FAIL a later round SUPERSEDES — the rule vendored
    from pr-reviewer-plugin#234, the same answer that repo's `QA panel` gate gives."""

    def decide(self, reviews):
        return rah.decide(reviews, HEAD, [])

    def test_a_round_that_refutes_every_blocking_finding_with_evidence_supersedes_the_fail(self):
        # mythxengine-sdk#409: r1 PASS, r2 FAIL with one major, r3 on the SAME head refutes it.
        decision = self.decide([r1(), r2(), round_review("PASS", id=103, record=refuting())])
        self.assertTrue(decision.ok)
        self.assertIn("refuted with evidence", decision.description)

    def test_the_fail_stands_unless_every_condition_holds(self):
        cases = {
            "open": round_review("PASS", id=103, record=refuting(d="open")),
            "fixed-on-an-unchanged-head": round_review("PASS", id=103, record=refuting(d="fixed")),
            "unaccounted": round_review("PASS", id=103, record={"of": 102, "rows": []}),
            "no-evidence": round_review("PASS", id=103, record=refuting(e=False)),
            "not-honoured": round_review("PASS", id=103, record=refuting(h=False)),
            "incomplete": round_review("PASS", id=103, record=refuting(), complete=False),
            "unverified": round_review("PASS", id=103, record=refuting(), verified=False),
            "no-record": round_review("PASS", id=103),
            "still-carried": round_review("PASS", [{**MAJOR, "carried": True}], id=103, record=refuting()),
        }
        for name, newer in cases.items():
            with self.subTest(name):
                decision = self.decide([r1(), r2(), newer])
                self.assertFalse(decision.ok)
                self.assertIn("FAIL", decision.description)

    def test_two_racing_rounds_on_one_head_settle_strictest(self):
        # pr-reviewer-plugin#89: both started from r1, so neither names the other.
        racer = round_review("PASS", id=103, record=refuting(of=101))
        for reviews in ([r1(), r2(), racer], [r1(), racer, r2()]):
            self.assertFalse(self.decide(reviews).ok)

    def test_the_strictest_round_wins_not_the_latest(self):
        # The old reading: after a FAIL, a re-review PASS turned this check green while the
        # plugin's `QA panel` stayed red.
        self.assertFalse(self.decide([r2(), round_review("PASS", id=103)]).ok)

    def test_a_promotion_speaks_only_for_a_head_with_no_round(self):
        promotion = round_review("PASS", id=104, promoted=True)
        self.assertTrue(self.decide([promotion]).ok)
        self.assertFalse(self.decide([r2(), promotion]).ok)

    def test_a_rule_that_raises_supersedes_nothing(self):
        def boom(_rounds):
            raise RuntimeError("rule broke")

        reviews = [r1(), r2(), round_review("PASS", id=103, record=refuting())]
        with mock.patch.object(rah, "_v_superseded_fails", boom), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.decide(reviews).ok)

    def test_the_vendored_block_is_unedited(self):
        # Edit the rule in pr-reviewer-plugin and re-sync; never here.
        text = Path(rah.__file__).read_text()
        begin = text.index("# ── BEGIN VENDORED SUPERSEDE RULE")
        end = text.index("\n", text.index("# ── END VENDORED SUPERSEDE RULE"))
        block = text[begin:end]
        recorded = re.search(r"block-ast-sha256:\s*(\S+)", block).group(1)
        actual = hashlib.sha256(_canonical(ast.parse(block).body).encode()).hexdigest()
        self.assertEqual(actual, recorded)


class MainTests(unittest.TestCase):
    def _env(self, **extra):
        env = {"PR_NUMBER": "123", "HEAD_SHA": MERGED, "PR_LABELS": ""}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def test_a_gh_failure_in_single_pr_mode_still_exits_zero(self):
        # The STATUS is the signal, not the job's exit code: a transient `gh` failure must
        # not become a second red check that makes an API hiccup look like an unreviewed PR.
        def boom(*args, **kwargs):
            raise RuntimeError("gh api repos/o/r/pulls/123/reviews failed: 502 Bad Gateway")

        err = io.StringIO()
        with self._env(), mock.patch.object(rah, "_gh", boom), contextlib.redirect_stderr(err):
            os.environ.pop("DRY_RUN", None)
            self.assertEqual(rah.main(), 0)
        self.assertIn("502 Bad Gateway", err.getvalue())

    def test_dry_run_is_off_unless_explicitly_truthy(self):
        # bool("false") is True, so `DRY_RUN=false` used to ENABLE dry-run: no status
        # posted, nothing red, the merge gate silently off.
        seen: dict[str, bool] = {}

        def fake_check_pr(pr, head, labels, *, dry_run):
            seen["dry_run"] = dry_run

        cases = [(v, False) for v in ("false", "0", "no", "off", "")]
        cases += [(v, True) for v in ("1", "true", "yes", "on", "TRUE")]
        for value, expected in cases:
            with self.subTest(DRY_RUN=value):
                seen.clear()
                with self._env(DRY_RUN=value), mock.patch.object(rah, "check_pr", fake_check_pr):
                    self.assertEqual(rah.main(), 0)
                self.assertIs(seen["dry_run"], expected)


if __name__ == "__main__":
    unittest.main()
