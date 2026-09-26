#!/usr/bin/env python3
"""Health check: is the review gate actually producing verdicts?

Vera fails QUIETLY. An exhausted panel posts no verdict, escalates to her own inbox,
and — before pr-reviewer-plugin#54 — said nothing on the PR. Over her first five weeks
44 PRs exhausted and 24 of them merged with no review ever; nobody noticed, because the
only surface carrying that fact was an inbox with no reader. This is the alarm that
would have caught it in week one instead of week five.

Run it from the ava fleet cron (the operator API is tailnet/container-local, so a cloud
runner cannot reach it — same constraint as check_card_drift.py):

    python3 scripts/check_review_health.py --container vera

Exit 0 = healthy; exit 1 = a threshold tripped (prints which); exit 2 = could not reach
the agent (operational error, NOT a health verdict — a dead container is a different
alarm, and conflating them means an outage reads as a clean gate).

EVERYTHING THE EVAL REPORT COUNTS IS A LIFETIME AGGREGATE. That is the trap this script
walks into if it compares a counter against a fixed ceiling: `unreviewed_prs` counts PRs
that exhausted the panel and were never recovered, and nothing can ever remove an entry
— the only thing that clears one is a later posted verdict, and nobody re-reviews a
merged PR. When the metric first went live it read 33, of which 32 were merged and 1
closed: zero open, zero actionable, and permanently above any small ceiling.

So the counters here alarm on GROWTH, against the previous run's values (issue #33). A
monitor that is always red is a monitor people stop reading — the same failure as an
inbox with no reader, with extra steps. Rates, which cannot drift upward forever, keep
their absolute floors.

State is persisted so cron does not have to carry baselines between runs. The first run
records and reports; it cannot alarm on growth, and says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vera_api import operator_api_get, telemetry_rows

# New PRs that exhausted and have no verdict SINCE THE LAST RUN. Not zero because the
# backfill sweep recovers some on a delay — protoAgent#2546 sat unreviewed for ~35min
# before a later pass posted — so a run landing inside that window sees a transient +1.
# Sustained real loss shows up as growth that does not come back down.
MAX_UNREVIEWED_GROWTH = 2
# Escalations pile up in the agent's own inbox and are never drained (no read/ack
# concept), so this only ever grows too.
MAX_INBOX_GROWTH = 5
# A rate, not a counter — it can fall as well as rise, so an absolute floor is honest.
# Below this, the panel is failing more often than it is succeeding on some axis.
MIN_COMPLETION_RATE = 0.80

DEFAULT_STATE = Path.home() / ".cache" / "vera-review-health.json"

# The RECENT window (issue: 2026-09-25 replica stall). The lifetime counters above cannot
# see a bad evening — 800+ reviews average it away — so the telemetry JSONL is read for
# the last WINDOW_HOURS and judged on its own. Rows are few in six hours, so every rate
# has a minimum sample and every count is small and absolute.
WINDOW_HOURS = 6
WINDOW_MIN_ROWS = 5  # below this a rate is noise
MAX_INCOMPLETE_RATE = 0.40  # incomplete rounds / reviewed rounds in the window
MAX_STRUCTURAL_UNAVAILABLE = 3  # rounds whose structural lane was out
MAX_EXHAUSTIONS = 2  # rounds that never got a verdict
MAX_PANEL_RETRIES = 4  # attempts that failed a lane and were retried


def _load_state(path: Path) -> dict:
    """Previous run's counters. A missing or corrupt file is not an error — it means
    'no baseline yet', which suppresses the growth checks rather than failing the run."""
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict) -> str | None:
    """Persist counters for the next run. Returns a warning string on failure — an
    unwritable state file must not fail the health check, but it does mean every
    subsequent run is a 'first run' and the growth alarms never arm, so it is said out
    loud rather than swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        return None
    except OSError as exc:
        return f"could not write state to {path} ({exc}) — growth alarms stay disarmed until this is fixed"


def _growth(problems: list[str], label: str, now: int | None, before: object, limit: int, extra: str = "") -> None:
    """Alarm when a monotonic counter climbs by more than `limit` since the last run."""
    if now is None or not isinstance(before, int):
        return
    delta = now - before
    if delta > limit:
        problems.append(f"{label} grew {delta} since last check ({before} -> {now}, allowed +{limit}){extra}")


def window_health(rows: list[dict], *, now: float, hours: int = WINDOW_HOURS) -> tuple[list[str], str]:
    """(problems, summary) for the telemetry rows of the last ``hours``. Pure, so the
    thresholds are testable. A window with too few reviewed rows judges counts only."""
    since = now - hours * 3600
    recent = [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= since]
    reviewed = [r for r in recent if r.get("event") == "reviewed"]
    incomplete = [r for r in reviewed if not r.get("complete", True)]
    structural = [r for r in reviewed if r.get("structural_unavailable")]
    exhaustions = [r for r in recent if r.get("event") == "exhaustion"]
    retries = [r for r in recent if r.get("event") == "panel_retry"]
    problems: list[str] = []
    if len(reviewed) >= WINDOW_MIN_ROWS:
        rate = len(incomplete) / len(reviewed)
        if rate > MAX_INCOMPLETE_RATE:
            lanes: dict[str, int] = {}
            for r in incomplete:
                for lane in (r.get("incomplete_finders") or []) + (r.get("degraded") or []):
                    lanes[str(lane)] = lanes.get(str(lane), 0) + 1
                if r.get("structural_unavailable"):
                    lanes["find_structural"] = lanes.get("find_structural", 0) + 1
            worst = ", ".join(f"{k}×{v}" for k, v in sorted(lanes.items(), key=lambda kv: -kv[1])[:3])
            problems.append(
                f"{len(incomplete)}/{len(reviewed)} rounds incomplete in the last {hours}h "
                f"({rate:.0%} > {MAX_INCOMPLETE_RATE:.0%}){': ' + worst if worst else ''}"
            )
    if len(structural) > MAX_STRUCTURAL_UNAVAILABLE:
        reasons = sorted({str(r.get("structural_reason") or "?") for r in structural})
        problems.append(
            f"structural lane unavailable on {len(structural)} rounds in the last {hours}h "
            f"(allowed {MAX_STRUCTURAL_UNAVAILABLE}): {', '.join(reasons)}"
        )
    if len(exhaustions) > MAX_EXHAUSTIONS:
        prs = sorted({f"{str(r.get('repo') or '').split('/')[-1]}#{r.get('pr')}" for r in exhaustions})
        problems.append(f"{len(exhaustions)} exhausted rounds in the last {hours}h (allowed {MAX_EXHAUSTIONS}): {', '.join(prs)}")
    if len(retries) > MAX_PANEL_RETRIES:
        lanes = sorted({str(x) for r in retries for x in (r.get("failed") or [])})
        problems.append(f"{len(retries)} panel retries in the last {hours}h (allowed {MAX_PANEL_RETRIES}): {', '.join(lanes)}")
    summary = (
        f"window{hours}h: reviewed={len(reviewed)} incomplete={len(incomplete)} "
        f"structural_out={len(structural)} exhaustions={len(exhaustions)} retries={len(retries)}"
    )
    return problems, summary


def _window_days(now: float, hours: int) -> list[str]:
    """The telemetry files a window can span — it rolls at midnight, so up to two days."""
    import datetime as _dt

    end = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
    start = _dt.datetime.fromtimestamp(now - hours * 3600, _dt.timezone.utc)
    days = [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")]
    return days if days[0] != days[1] else days[:1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera", help="container name (default: vera)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE, help=f"state file (default: {DEFAULT_STATE})")
    ap.add_argument("--baseline", type=int, default=None, help="override the stored inbox baseline")
    ap.add_argument("--max-unreviewed-growth", type=int, default=MAX_UNREVIEWED_GROWTH)
    ap.add_argument("--max-inbox-growth", type=int, default=MAX_INBOX_GROWTH)
    ap.add_argument("--min-completion", type=float, default=MIN_COMPLETION_RATE)
    ap.add_argument("--no-save", action="store_true", help="do not update the state file (dry run)")
    ap.add_argument("--window-hours", type=int, default=WINDOW_HOURS, help="recent-window length (0 disables)")
    args = ap.parse_args()

    try:
        report = operator_api_get(args.container, "/api/plugins/pr-reviewer/eval")
        inbox = operator_api_get(args.container, "/api/inbox")
    except Exception as exc:  # noqa: BLE001 — any failure here is operational, not a verdict
        print(f"UNREACHABLE: {exc}", file=sys.stderr)
        return 2

    prev = _load_state(args.state)
    problems: list[str] = []
    notes: list[str] = []

    # The headline metric (pr-reviewer-plugin#67). Absent on a plugin older than the
    # version that added it — fall back to the inbox, the only other place the number
    # exists. This stays a soft degrade on purpose: a health check that hard-fails on an
    # older plugin turns a routine version skew into a page.
    unreviewed_block = report.get("unreviewed_prs") or {}
    unreviewed = unreviewed_block.get("count")
    if unreviewed is None:
        notes.append("eval report has no `unreviewed_prs` (plugin predates #67) — using inbox depth only")
    else:
        # Name the arrivals when we can. The report caps its `prs` list, so a diff of
        # names is only complete while the count is under that cap; past it the count is
        # still exact and the names are a sample. Say which it is.
        listed = unreviewed_block.get("prs") or []
        new = sorted(set(listed) - set(prev.get("unreviewed_prs") or []))
        sample = " (names sampled — report truncates the list)" if unreviewed > len(listed) else ""
        _growth(
            problems,
            "unreviewed PRs (exhausted, no verdict)",
            unreviewed,
            prev.get("unreviewed"),
            args.max_unreviewed_growth,
            f": {', '.join(new)}{sample}" if new else sample,
        )

    depth = len(inbox.get("items") or [])
    baseline = args.baseline if args.baseline is not None else prev.get("inbox")
    _growth(problems, "escalation inbox", depth, baseline, args.max_inbox_growth)

    rate = report.get("completion_rate")
    if isinstance(rate, (int, float)) and rate < args.min_completion:
        problems.append(f"completion rate {rate:.2%} below {args.min_completion:.0%}")

    # The recent window: the one place a bad evening shows before the lifetime averages move.
    window_summary = ""
    if args.window_hours > 0:
        import time as _time

        now = _time.time()
        try:
            rows = telemetry_rows(args.container, _window_days(now, args.window_hours))
        except Exception as exc:  # noqa: BLE001 — the eval report already answered; say so, don't fail
            notes.append(f"telemetry unreadable ({exc}) — recent-window checks skipped this run")
        else:
            window_problems, window_summary = window_health(rows, now=now, hours=args.window_hours)
            problems.extend(window_problems)

    if not prev:
        notes.append(f"first run (no state at {args.state}) — recording baselines; growth alarms arm next run")

    if not args.no_save:
        state = {"inbox": depth, "unreviewed": unreviewed, "unreviewed_prs": unreviewed_block.get("prs") or []}
        if warning := _save_state(args.state, state):
            notes.append(warning)

    summary = (
        f"dispatches={report.get('dispatches')} posted={report.get('reviews_posted')} "
        f"completion={rate} exhaustions={report.get('exhaustions')} "
        f"unreviewed={unreviewed} (lifetime) inbox={depth}"
    ) + (f" | {window_summary}" if window_summary else "")
    for n in notes:
        print(f"note: {n}")
    if problems:
        print(f"UNHEALTHY: {summary}")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ok: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
