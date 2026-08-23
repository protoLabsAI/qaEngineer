#!/usr/bin/env python3
"""Stopgap: bound Vera's checkout cache, because the plugin's own pruner never runs.

`CheckoutCache.prune()` exists in pr-reviewer, is documented in its module docstring,
and is unit-tested — and nothing in the plugin ever calls it (filed as
pr-reviewer-plugin#87). Measured consequence on this deployment: 43 GiB across 1248
entries against the module's own 5 GiB / 50-entry / 1-hour-TTL caps, with 1247 of those
entries already past the TTL. It was the largest consumer on the host and took it to
92% disk. After a manual sweep the cache regrew ~0.5 GiB in 90 minutes (~8 GiB/day), so
this is not a one-time cleanup — it needs a schedule until the upstream fix lands.

DELETE THIS SCRIPT once #87 ships and the plugin prunes itself. It exists only because
a cache with a documented, tested, uncalled pruner is indistinguishable from a cache
with no pruner at all — and the disk is where you find out.

POLICY, and why it is not simply the plugin's TTL:

  * Keep the N newest entries per repo (default 3). A pure TTL sweep would evict a
    checkout the very next review re-clones — the cache exists so an unchanged head
    reaffirms in under a second, and buying disk with latency on every repo is a bad
    trade. Three covers the head plus a re-review or two.
  * Keep ANYTHING touched inside the protect window (default 1h), regardless of count.
    Reviews run 3-10 minutes and several can be in flight; deleting a checkout out from
    under a running panel is the one way this script could cause the failure it exists
    to prevent. The window is deliberately much longer than the longest observed review.

Both rules are additive — an entry survives if EITHER holds.

Usage (dry run by default; --apply to delete):
    python3 scripts/prune_checkout_cache.py --container vera
    python3 scripts/prune_checkout_cache.py --container vera --apply

Exit 0 = swept (or nothing to do), 2 = could not reach the container.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

CACHE_ROOT = "/sandbox/pr-reviewer/checkouts"
DEFAULT_KEEP_PER_REPO = 3
DEFAULT_PROTECT_MIN = 60

# Runs INSIDE the container: the cache lives on a named volume owned by uid 1001, and
# reaching it from the host would mean guessing the volume mountpoint and the uid. The
# body is kept dependency-free (stdlib only) because the image's python is not ours to
# add packages to.
_SWEEP = r"""
import json, os, shutil, sys, time
ROOT, KEEP, PROTECT, APPLY = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4] == "apply"
now = time.time()
def dsize(p):
    n = 0
    for dp, _, fs in os.walk(p):
        for f in fs:
            try: n += os.lstat(os.path.join(dp, f)).st_size
            except OSError: pass
    return n
deleted = kept = 0
freed = 0
errors = []
if os.path.isdir(ROOT):
    for repo in sorted(os.listdir(ROOT)):
        rp = os.path.join(ROOT, repo)
        if not os.path.isdir(rp): continue
        try:
            ents = [(os.path.getmtime(os.path.join(rp, d)), os.path.join(rp, d))
                    for d in os.listdir(rp) if os.path.isdir(os.path.join(rp, d))]
        except OSError as e:
            errors.append(f"{repo}: {e}"); continue
        ents.sort(reverse=True)
        keep = {p for _, p in ents[:KEEP]} | {p for m, p in ents if now - m < PROTECT}
        for _, p in ents:
            if p in keep:
                kept += 1
                continue
            freed += dsize(p)
            deleted += 1
            if APPLY:
                shutil.rmtree(p, ignore_errors=True)
print(json.dumps({"deleted": deleted, "kept": kept, "freed_bytes": freed, "errors": errors[:5]}))
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera")
    ap.add_argument("--keep-per-repo", type=int, default=DEFAULT_KEEP_PER_REPO)
    ap.add_argument("--protect-min", type=float, default=DEFAULT_PROTECT_MIN)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    try:
        out = subprocess.run(
            [
                "docker", "exec", args.container, "python3", "-c", _SWEEP,
                CACHE_ROOT, str(args.keep_per_repo), str(args.protect_min * 60),
                "apply" if args.apply else "dry",
            ],
            capture_output=True, text=True, timeout=1800,
        )
        if out.returncode != 0:
            print(f"UNREACHABLE: docker exec {args.container} failed: {out.stderr.strip()[:300]}")
            return 2
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 — every failure here is operational
        print(f"UNREACHABLE: {exc}")
        return 2

    gib = result["freed_bytes"] / 1024**3
    verb = "deleted" if args.apply else "would delete"
    print(f"{verb} {result['deleted']} checkout entries ({gib:.1f} GiB), kept {result['kept']}")
    for e in result.get("errors") or []:
        print(f"  warning: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
