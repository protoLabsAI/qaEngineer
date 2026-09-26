#!/usr/bin/env python3
"""Post-roll smoke: prove the whole panel still runs end-to-end after an image roll.

The sweep alone only proves promotion logic runs. The replay route runs the full recipe —
workflow engine, four LLM finders, the clawpatch structural lane, verify + grounding,
report — without posting anything to GitHub, which makes it the one honest post-upgrade
check. Until 2026-09-26 it was a hand-run curl, and that run replayed a PR that had merged
four minutes earlier: the merged-PR trap (pr-reviewer-plugin#84) is a replay that comes
back PASS / 0 findings in seconds because the head yields nothing to review, which is
exactly the wrong bug for a post-upgrade check to have. So this script (a) picks an OPEN
PR and re-checks it is still open afterwards, and (b) refuses to believe a result whose
finder lanes finished suspiciously fast.

    python3 scripts/smoke_replay.py --container vera                   # picks a PR
    python3 scripts/smoke_replay.py --repo protoLabsAI/protoAgent --pr 3626

Exit 0 = every lane ran for real; exit 1 = the smoke failed (prints why); exit 2 = could
not reach the agent or GitHub (operational, not a verdict). A replay spends a full panel
(~5–15 min of LLM time) — run it at an idle moment, never during live-review load.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from vera_api import operator_api_post

FINDER_STEPS = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions", "find_structural")
PANEL_STEPS = FINDER_STEPS + ("synthesize", "verify", "report")
# A real finder pass over a real diff takes minutes; the merged-head shape came back in
# 4–7 s per lane (memory of the 2026-08-22 incident). 30 s is far under the slowest honest
# lane seen (162 s on an 8-file PR) and far over the trap.
MIN_FINDER_S = 30.0
# Pick a PR big enough that the lanes have something to do and small enough to finish.
MIN_LINES, MAX_LINES = 20, 1500
DEFAULT_REPOS = ("protoLabsAI/protoAgent", "protoLabsAI/pr-reviewer-plugin", "protoLabsAI/qaEngineer")


def evaluate(result: dict, *, min_finder_s: float = MIN_FINDER_S) -> tuple[list[str], str]:
    """(problems, one-line summary) for a replay response. Pure, so the rule is testable.

    Fails on: no run; `empty_diff`; any `failed_steps`; a panel step missing from
    `step_seconds`; a finder lane under the floor. `degraded_steps` is reported, not failed
    — a lane the plugin degraded on purpose (context overrun) is the machinery working.
    """
    runs = result.get("runs") if isinstance(result, dict) else None
    if not runs or not isinstance(runs[0], dict):
        return [f"no run in the replay response: {json.dumps(result)[:200]}"], "no run"
    run = runs[0]
    tel = run.get("telemetry") or {}
    problems: list[str] = []
    if tel.get("empty_diff"):
        problems.append("empty_diff=true — the head had nothing to review (merged/rebased PR?)")
    failed = list(tel.get("failed_steps") or [])
    if failed:
        problems.append(f"failed steps: {', '.join(map(str, failed))}")
    seconds = tel.get("step_seconds") or {}
    missing = [s for s in PANEL_STEPS if s not in seconds]
    if missing:
        problems.append(f"steps that never ran: {', '.join(missing)}")
    fast = [f"{s}={seconds[s]:.0f}s" for s in FINDER_STEPS if s in seconds and float(seconds[s]) < min_finder_s]
    if fast:
        problems.append(f"finder lane(s) under the {min_finder_s:.0f}s floor (merged-head trap?): {', '.join(fast)}")
    verdict = run.get("verdict")
    findings = run.get("findings") or []
    degraded = list(tel.get("degraded_steps") or [])
    slowest = max(seconds, key=seconds.get) if seconds else "-"
    summary = (
        f"verdict={verdict} findings={len(findings)} degraded={degraded or '-'} "
        f"slowest={slowest}:{seconds.get(slowest, 0):.0f}s structural={seconds.get('find_structural', 0):.0f}s"
    )
    return problems, summary


def _gh_json(args: list[str]) -> object:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "null")


def pick_pr(repos: tuple[str, ...]) -> tuple[str, int, str] | None:
    """The most recently updated open, non-draft PR within the size window — (repo, pr, head)."""
    for repo in repos:
        prs = _gh_json(
            [
                "pr",
                "list",
                "-R",
                repo,
                "--state",
                "open",
                "--json",
                "number,headRefOid,additions,deletions,isDraft,updatedAt",
                "-L",
                "30",
            ]
        )
        for pr in sorted(prs or [], key=lambda p: p.get("updatedAt") or "", reverse=True):
            size = int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
            if not pr.get("isDraft") and MIN_LINES <= size <= MAX_LINES:
                return repo, int(pr["number"]), str(pr["headRefOid"])
    return None


def pr_state(repo: str, pr: int) -> tuple[str, str]:
    data = _gh_json(["pr", "view", str(pr), "-R", repo, "--json", "state,headRefOid"]) or {}
    return str(data.get("state") or "?"), str(data.get("headRefOid") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera")
    ap.add_argument("--repo", help="owner/name (with --pr); default: pick an open PR from the managed repos")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--min-finder-s", type=float, default=MIN_FINDER_S)
    ap.add_argument("--timeout-s", type=int, default=3000, help="panel wall clock to wait for")
    args = ap.parse_args()

    try:
        if args.repo and args.pr:
            state, head = pr_state(args.repo, args.pr)
            if state != "OPEN":
                print(f"REFUSED: {args.repo}#{args.pr} is {state}, not OPEN — a merged head replays as PASS/0", file=sys.stderr)
                return 1
            target = (args.repo, args.pr, head)
        else:
            target = pick_pr(DEFAULT_REPOS)
            if target is None:
                print("UNREACHABLE: no open PR in the size window across the default repos", file=sys.stderr)
                return 2
    except Exception as exc:  # noqa: BLE001
        print(f"UNREACHABLE: {exc}", file=sys.stderr)
        return 2
    repo, pr, head = target
    print(f"replaying {repo}#{pr} @{head[:12]} (full panel, nothing is posted) …", flush=True)
    try:
        result = operator_api_post(
            args.container,
            "/api/plugins/pr-reviewer/replay",
            {"row": {"repo": repo, "pr": pr, "head": head}},
            timeout_s=args.timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"UNREACHABLE: {exc}", file=sys.stderr)
        return 2
    problems, summary = evaluate(result, min_finder_s=args.min_finder_s)
    try:
        state_after, head_after = pr_state(repo, pr)
        if state_after != "OPEN" or head_after != head:
            problems.append(f"PR changed during the replay (state={state_after}, head={head_after[:12]}) — rerun")
    except Exception as exc:  # noqa: BLE001
        print(f"note: could not re-check PR state after the replay ({exc})")
    if problems:
        print(f"SMOKE FAILED on {repo}#{pr}: {summary}")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ok: {repo}#{pr} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
