"""The `Review at head` gate — scripts/review_at_head.py.

This script is the sole required context on `main`, so these are tests of a GUARD: each
case is a way the gate used to pass a head it should not have (qaEngineer#73, ported from
protoLabsAI/protoAgent#3543), plus the incident it exists to catch — a verdict for an older
head must not satisfy the merged one.

Stdlib unittest, like test_watchdog_checks.py: CI here is python3 + PyYAML. protoAgent's
copy of this suite is pytest; the cases below are the same ones, restated.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
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
