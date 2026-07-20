#!/usr/bin/env python3
"""Drift check: does the RUNNING agent card match the seed config-as-code?

The config volume is seed-once (PROTOAGENT_SEED_CONFIG copies the seed on first
boot only, never merges after), so an edit to deploy/vera.langgraph-config.yaml
reaches fresh instances but NOT an already-running one — the live card silently
keeps stale/template values. This asserts the live card reflects the seed, so
that gap fails loudly instead of being discovered by accident.

Run it from the ava fleet cron (the card is tailnet-only, so a cloud CI runner
can't reach it — the STATIC half of this check lives in CI/ci.yml instead):

    python3 scripts/check_card_drift.py \
        --card-url http://100.101.189.45:7874/.well-known/agent-card.json \
        --seed deploy/vera.langgraph-config.yaml

Exit 0 = in sync; exit 1 = drift (prints what diverged); exit 2 = couldn't reach
the card / parse inputs (operational error, not a drift verdict).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import yaml

TEMPLATE_MARKERS = ("protoagent template", "replace this description", "replace with your agent")
DEFAULT_CARD_URL = "http://100.101.189.45:7874/.well-known/agent-card.json"
DEFAULT_SEED = "deploy/vera.langgraph-config.yaml"


def _looks_template(text: str) -> bool:
    low = (text or "").lower()
    return not text.strip() or any(m in low for m in TEMPLATE_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--card-url", default=DEFAULT_CARD_URL)
    ap.add_argument("--seed", default=DEFAULT_SEED)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    # Expected identity from the seed (source of truth)
    try:
        seed = yaml.safe_load(Path(args.seed).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"error: cannot read seed {args.seed}: {e}", file=sys.stderr)
        return 2
    a2a = seed.get("a2a") or {}
    want_skills = {s["id"] for s in (a2a.get("skills") or []) if s.get("id")}
    want_desc = (a2a.get("description") or "").strip()

    # Live card
    try:
        with urllib.request.urlopen(args.card_url, timeout=args.timeout) as r:
            card = json.load(r)
    except Exception as e:  # noqa: BLE001 — any failure to reach/parse is operational
        print(f"error: cannot fetch card {args.card_url}: {e}", file=sys.stderr)
        return 2

    have_desc = (card.get("description") or "").strip()
    have_skills = {s.get("id") for s in (card.get("skills") or []) if s.get("id")}

    drift: list[str] = []
    if _looks_template(have_desc):
        drift.append(f"description is still the stock template: {have_desc[:70]!r}")
    elif want_desc and have_desc != want_desc:
        drift.append("description differs from seed:\n"
                     f"    seed: {want_desc[:80]!r}\n    live: {have_desc[:80]!r}")
    missing = want_skills - have_skills
    if missing:
        drift.append(f"card is missing seed-declared skills: {sorted(missing)} "
                     f"(live has {sorted(have_skills)})")

    name = card.get("name", "?")
    if drift:
        print(f"DRIFT — {name} card does not match {args.seed}:")
        for d in drift:
            print(f"  - {d}")
        print("\nApply the seed to the running instance (re-seed the config volume, "
              "or POST /api/config).")
        return 1

    print(f"OK — {name} card matches the seed "
          f"(description set, skills {sorted(have_skills) or '[]'}, version {card.get('version')}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
