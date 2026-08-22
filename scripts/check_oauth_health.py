#!/usr/bin/env python3
"""Health check: is Vera's Claude subscription credential still one she can use?

The companion to check_model_fallback.py, and deliberately a different question. That
script asks "did she already degrade?" (observed, after the fact). This one asks "is the
thing that would make her degrade still sound?" (the cause, before the fact). Both are
needed: the credential can be perfect and the lane still fall back on a 529, and the
credential can be dead for hours on an idle agent with no traffic to reveal it.

WHY NOT JUST CALL /api/config/test-model. Because it lies in the direction that matters.
It streams a real 1-token turn through the subscription, which sounds like the perfect
liveness probe — but measured on 2026-08-21 it returned `429 rate_limit_error` on four
consecutive attempts WHILE a real A2A turn on the same credential completed fine and
telemetry recorded `model=claude-sonnet-5`. Wiring the alert to that probe would have
paged #alerts immediately and permanently, for a lane that was working. A monitor whose
false-positive rate is 100% on day one is worse than no monitor. So this check reads
STATE, not liveness, and leaves "did it actually degrade" to the fallback detector,
which is grounded in traffic that really happened.

What it checks, all from `/api/config` + `/api/config/oauth-status` (core ≥0.137.1,
where protoAgent#2564 started publishing expiry and refreshability):

  * signed_in — the credential is gone or was disconnected. Every call raises; the
    fallback lane carries 100% of her traffic. This is the "OAuth expired" case.
  * refreshable — a credential that cannot refresh is a deadline, not a credential.
    This is also the CLAUDE_CODE_OAUTH_TOKEN trap: that env path is never refreshed and
    never inspectable, and reads `signed_in: true` right up until it 401s.
  * provider/name coherence — protoAgent#2623 made model.name and model.provider ONE
    decision; a native-OAuth provider with a namespaced name ("protolabs/smart") is
    rejected by the native builder on every call. That config is silently fatal, and it
    is exactly what a careless half-edit of the model config produces.
  * expires_at — reported always, alarmed on only when the credential is NOT
    refreshable. Refresh is ON USE, so a healthy busy agent legitimately sits near its
    expiry all day; alarming on proximity alone would cry wolf every few hours.

Run from the ava fleet cron:

    python3 scripts/check_oauth_health.py --container vera

Exit 0 = credential sound; exit 1 = a real problem (prints which); exit 2 = could not
reach the agent (operational, NOT a verdict).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

# Native-OAuth providers (ADR 0097). A gateway-backed agent has no credential of its own
# to check — this whole script is a no-op for one, and says so rather than passing mutely.
NATIVE_PROVIDERS = {"anthropic-oauth", "openai-codex"}


def _api(container: str, path: str) -> dict:
    cmd = f'curl -s -m 25 -H "Authorization: Bearer $A2A_AUTH_TOKEN" localhost:7870{path}'
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c", cmd], capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed for {path}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def evaluate(model_cfg: dict, providers: list[dict]) -> tuple[int, list[str]]:
    """(exit_code, report lines). Pure over the two API bodies so the rules are testable."""
    lines: list[str] = []
    provider = (model_cfg.get("provider") or "").strip().lower()
    name = (model_cfg.get("name") or "").strip()

    if provider not in NATIVE_PROVIDERS:
        return 0, [f"OK: model.provider={provider!r} is not a native OAuth lane — no credential to check."]

    lines.append(f"lane: {provider} · {name}")
    problems: list[str] = []

    # #2623 — the two halves are one decision, and a mismatched pair fails every call.
    if "/" in name:
        problems.append(
            f"model.name={name!r} is a gateway alias but model.provider={provider!r} is native — "
            "the native builder rejects any name containing '/', so EVERY call raises and the "
            "fallback lane is carrying all traffic. Set model.name and model.provider together."
        )

    status = next((p for p in providers if (p.get("provider") or "").lower() == provider), None)
    if status is None:
        problems.append(f"/api/config/oauth-status reports nothing for {provider!r}")
        return 1, lines + problems

    if not status.get("signed_in"):
        problems.append(
            f"NOT SIGNED IN ({status.get('detail') or 'no detail'}) — re-auth with "
            f"POST /api/config/oauth/start {{\"provider\":\"{provider}\"}} and approve on any device."
        )
    else:
        source = status.get("source") or "?"
        durability = status.get("durability") or "?"
        lines.append(f"signed in · source={source} · durability={durability}")
        if not status.get("refreshable"):
            problems.append(
                f"credential is NOT refreshable (source={source}) — it is a deadline, not a "
                "credential. If this is CLAUDE_CODE_OAUTH_TOKEN, drop the env var and sign in "
                "through /api/config/oauth/start so protoAgent owns a refreshing copy."
            )
        expires_at = status.get("expires_at")
        if expires_at:
            remaining_h = (float(expires_at) - time.time()) / 3600.0
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(expires_at)))
            lines.append(f"access token expires {when} ({remaining_h:+.1f}h) — refreshed on use")

    return (1 if problems else 0), lines + problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera")
    args = ap.parse_args()

    try:
        model_cfg = _api(args.container, "/api/config").get("config", {}).get("model", {})
        providers = _api(args.container, "/api/config/oauth-status").get("providers", [])
    except Exception as exc:  # noqa: BLE001 — operational, exit 2
        print(f"UNREACHABLE: {exc}")
        return 2

    code, lines = evaluate(model_cfg, providers)
    print(("FAIL: " if code else "OK: ") + lines[0])
    for line in lines[1:]:
        print(f"  {line}")
    return code


if __name__ == "__main__":
    sys.exit(main())
