#!/usr/bin/env python3
"""Health check: is the review gate actually producing verdicts?

Vera fails QUIETLY. An exhausted panel posts no verdict, escalates to her own inbox,
and — before pr-reviewer-plugin#54 — said nothing on the PR. Over her first five weeks
44 PRs exhausted and 24 of them merged with no review ever; nobody noticed, because the
only surface carrying that fact was an inbox with no reader. This is the alarm that
would have caught it in week one instead of week five.

Run it from the ava fleet cron (the operator API is tailnet/container-local, so a cloud
runner cannot reach it — same constraint as check_card_drift.py):

    docker exec vera sh -c 'curl -s -H "Authorization: Bearer $A2A_AUTH_TOKEN" \
        localhost:7870/api/plugins/pr-reviewer/eval' \
      | python3 scripts/check_review_health.py --inbox-depth "$(...)"

or let it drive docker itself:

    python3 scripts/check_review_health.py --container vera

Exit 0 = healthy; exit 1 = a threshold tripped (prints which); exit 2 = could not reach
the agent (operational error, NOT a health verdict — a dead container is a different
alarm, and conflating them means an outage reads as a clean gate).

Thresholds are deliberately loose. This exists to catch a gate that has stopped gating,
not to page on a slow afternoon.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# An exhausted PR that no later pass reviewed. The gate did not run and nothing said so.
# Any sustained non-zero here is the failure this script exists for.
MAX_UNREVIEWED = 3
# Escalations pile up in the agent's own inbox and are never drained (no read/ack
# concept), so this only ever grows. Alarm on the RATE via --baseline, not the total.
MAX_INBOX_GROWTH = 5
# Below this, the panel is failing more often than it is succeeding on some axis.
MIN_COMPLETION_RATE = 0.80


def _api(container: str, path: str) -> dict:
    """Read a token-gated operator-API endpoint from inside the container."""
    cmd = f'curl -s -m 25 -H "Authorization: Bearer $A2A_AUTH_TOKEN" localhost:7870{path}'
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c", cmd], capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed for {path}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera", help="container name (default: vera)")
    ap.add_argument("--baseline", type=int, default=None, help="previous inbox depth, to alarm on growth")
    ap.add_argument("--max-unreviewed", type=int, default=MAX_UNREVIEWED)
    ap.add_argument("--min-completion", type=float, default=MIN_COMPLETION_RATE)
    args = ap.parse_args()

    try:
        report = _api(args.container, "/api/plugins/pr-reviewer/eval")
        inbox = _api(args.container, "/api/inbox")
    except Exception as exc:  # noqa: BLE001 — any failure here is operational, not a verdict
        print(f"UNREACHABLE: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []

    # The headline metric (pr-reviewer-plugin#67). Absent on a plugin older than the
    # version that added it — fall back to the inbox, the only other place the number
    # exists. This stays a soft degrade on purpose: a health check that hard-fails on an
    # older plugin turns a routine version skew into a page.
    unreviewed = (report.get("unreviewed_prs") or {}).get("count")
    if unreviewed is None:
        print("note: eval report has no `unreviewed_prs` (plugin predates #67) — using inbox depth only")
    elif unreviewed > args.max_unreviewed:
        prs = ", ".join((report.get("unreviewed_prs") or {}).get("prs", [])[:5])
        problems.append(f"{unreviewed} PR(s) exhausted with no verdict (> {args.max_unreviewed}): {prs}")

    depth = len(inbox.get("items") or [])
    if args.baseline is not None and depth - args.baseline > MAX_INBOX_GROWTH:
        problems.append(f"escalation inbox grew {depth - args.baseline} since last check (now {depth})")

    rate = report.get("completion_rate")
    if isinstance(rate, (int, float)) and rate < args.min_completion:
        problems.append(f"completion rate {rate:.2%} below {args.min_completion:.0%}")

    summary = (
        f"dispatches={report.get('dispatches')} posted={report.get('reviews_posted')} "
        f"completion={rate} exhaustions={report.get('exhaustions')} "
        f"unreviewed={unreviewed} inbox={depth}"
    )
    if problems:
        print(f"UNHEALTHY: {summary}")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ok: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
