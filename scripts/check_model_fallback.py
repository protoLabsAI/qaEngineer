#!/usr/bin/env python3
"""Health check: is Vera silently answering from her FALLBACK model?

`routing.fallback_models` is Vera's only degrade path, and when it fires it fires
SILENTLY. protoAgent wires langchain's `ModelFallbackMiddleware` raw (graph/agent.py),
and that middleware swallows the primary's exception with no log, no counter, and no
event — a successful fallback is byte-for-byte indistinguishable from a normal turn.
Filed upstream as protoAgent#2956 and **FIXED in core 0.145.0** — which this repo now
pins: `ObservableModelFallbackMiddleware` logs a WARNING and publishes a `model.fallback`
bus event (ADR 0039). So this script is already living on borrowed time, deliberately:

RETIRE IT once the RUNNING instance is on 0.145.0 *and* the event has been seen firing.
Pinning a version is not the same as running it — Vera rolls on watchtower after this
merges, and until then she is on 0.137.1 with no event at all. Deleting the inference
before its replacement is observed working would leave the silent-degrade window
uncovered by both, which is the one outcome worth avoiding. Once the event is confirmed,
delete this file: the event is ground truth where this is an inference from traffic.

THE INFERENCE. Since 2026-08-21 Vera's primary is a NATIVE OAUTH provider
(`model.provider: anthropic-oauth`, claude-sonnet-5), which per ADR 0097 bypasses the
gateway entirely — her subscription traffic never touches LiteLLM. Her fallback is a
gateway alias (`protolabs/smart`), reachable only because #2571 lets a namespaced slot
name opt out of the native provider. So the two lanes are cleanly separable at the
gateway, and the rule is simply:

    a protoAgent-UA chat completion from Vera's container IP == a fallback

Two things share that gateway key and must NOT be counted:

  * clawpatch (the protoPatch structural engine) — the pr-reviewer plugin exports
    OPENAI_API_KEY to the subprocess, and it calls protolabs/smart too. It is a node
    process, so it lands under `user_agent="node"` while protoAgent's own calls carry
    `user_agent="protoAgent/0.1 (+...)"`. That label is the whole discriminator.
  * embeddings (`qwen3-embedding`) — a different route and a different model; knowledge
    embeddings are off today but that can be flipped from the console, so filter by
    route rather than trusting the config.

ALARM ON GROWTH, NOT ON DEPTH. `litellm_proxy_total_requests_metric_total` is a
lifetime counter, so a fixed ceiling would latch red forever after the first bad hour —
the same trap check_review_health.py documents. State carries the previous run's total;
a run alarms only on NEW fallback traffic since the last one.

COOLDOWN, because the loud failure here is not one 429. A subscription that is
rate-limited stays rate-limited for a window, and every review in that window falls
back — alerting per run would post to #alerts every few minutes for an hour and train
everyone to mute the channel. After an alert, stay quiet for --cooldown-min while
still tracking the counter, then re-alarm if it is STILL growing. Sustained degradation
gets through; a burst gets one post.

Run it from the ava fleet cron (the gateway's metrics port is container-local):

    python3 scripts/check_model_fallback.py --container vera

Exit 0 = primary lane healthy (or inside a cooldown); exit 1 = fallback traffic since
the last run; exit 2 = could not reach the gateway or the container (an operational
error, NOT a verdict — a dead scraper must not read as a clean lane).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_STATE = Path.home() / ".cache" / "vera-model-fallback.json"
# The gateway publishes on the shared ai_default net; from ava's host namespace it is
# reachable on the published port. Overridable for a different host/stack.
DEFAULT_METRICS = "http://localhost:4000/metrics/"
# protoAgent stamps its own User-Agent on every model call it makes; clawpatch (node)
# and ad-hoc curl do not. This prefix IS the "was it the agent itself" test.
AGENT_UA_PREFIX = "protoAgent"
# Embeddings ride the same key and container but are not a chat lane.
EMBEDDING_ROUTE = "/v1/embeddings"
DEFAULT_COOLDOWN_MIN = 60

_SAMPLE = re.compile(r"^litellm_proxy_total_requests_metric_total\{(?P<labels>.*)\}\s+(?P<value>[0-9.eE+-]+)$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _container_ip(container: str) -> str:
    """Vera's current address on the gateway's network.

    Resolved fresh every run on purpose: a watchtower roll gives her a new IP, and a
    hardcoded one would silently stop matching — the counter would flatline and the
    check would report a healthy lane forever. That is the exact failure class this
    script exists to catch, so it must not reproduce it.
    """
    out = subprocess.run(
        ["docker", "inspect", container, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker inspect {container} failed: {out.stderr.strip()[:200]}")
    ips = out.stdout.split()
    if not ips:
        raise RuntimeError(f"{container} has no container IP (is it running?)")
    return ips[0]


def _scrape(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"cannot scrape {url}: {exc}") from exc


def fallback_requests(metrics_text: str, agent_ip: str) -> tuple[float, dict[str, float]]:
    """Total protoAgent-UA gateway chat requests from ``agent_ip``, and the per-model split.

    Pure over the scrape text so the attribution rule is unit-testable without a live
    gateway — the rule is the load-bearing part, not the HTTP.
    """
    total = 0.0
    by_model: dict[str, float] = {}
    for line in metrics_text.splitlines():
        match = _SAMPLE.match(line.strip())
        if not match:
            continue
        labels = {k: v for k, v in _LABEL.findall(match.group("labels"))}
        if labels.get("client_ip") != agent_ip:
            continue
        if not labels.get("user_agent", "").startswith(AGENT_UA_PREFIX):
            continue
        if labels.get("route") == EMBEDDING_ROUTE:
            continue
        value = float(match.group("value"))
        total += value
        model = labels.get("requested_model", "?")
        by_model[model] = by_model.get(model, 0.0) + value
    return total, by_model


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="vera")
    ap.add_argument("--metrics-url", default=DEFAULT_METRICS)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--cooldown-min", type=float, default=DEFAULT_COOLDOWN_MIN)
    ap.add_argument("--no-save", action="store_true", help="do not update the stored baseline (test runs)")
    args = ap.parse_args()

    try:
        agent_ip = _container_ip(args.container)
        total, by_model = fallback_requests(_scrape(args.metrics_url), agent_ip)
    except Exception as exc:  # noqa: BLE001 — every failure here is operational, exit 2
        print(f"UNREACHABLE: {exc}")
        return 2

    state = _load_state(args.state)
    previous = state.get("total")
    now = time.time()
    last_alert = state.get("last_alert_ts", 0.0)
    split = ", ".join(f"{m}={int(v)}" for m, v in sorted(by_model.items())) or "(none)"

    new_state = dict(state)
    new_state["total"] = total
    new_state["checked_at"] = now
    new_state["agent_ip"] = agent_ip
    new_state["by_model"] = by_model

    verdict = 0
    if previous is None:
        print(f"BASELINE: {args.container} @ {agent_ip} — {int(total)} fallback requests so far [{split}]")
        print("First run: recorded. Growth is what alarms, so this run cannot.")
    elif total < previous:
        # The gateway restarted and its counters reset. Re-baseline rather than reading
        # the negative delta as "healthy" — and say so, because a silent re-baseline
        # across a restart could swallow a real burst.
        print(f"COUNTER RESET: gateway counters went {int(previous)} → {int(total)} (restart). Re-baselined.")
    else:
        delta = total - previous
        if delta <= 0:
            print(f"OK: no fallback traffic since the last run ({int(total)} lifetime) [{split}]")
        else:
            cooling = (now - last_alert) < args.cooldown_min * 60
            head = (
                f"{int(delta)} FALLBACK requests since the last run "
                f"({int(total)} lifetime) [{split}]"
            )
            if cooling:
                quiet_for = int((args.cooldown_min * 60 - (now - last_alert)) / 60)
                print(f"DEGRADED (cooldown, quiet ~{quiet_for}m more): {head}")
            else:
                print(f"FALLBACK: {head}")
                print(
                    f"Vera answered {int(delta)} model calls from her fallback lane, not "
                    "claude-sonnet-5. Check the subscription: "
                    "`curl -X POST localhost:7870/api/config/test-model` in the container "
                    "(401/403 = re-auth needed, 429 = rate-limited, it will pass)."
                )
                new_state["last_alert_ts"] = now
                verdict = 1

    if not args.no_save:
        try:
            _save_state(args.state, new_state)
        except OSError as exc:
            # Persisting the baseline is bookkeeping; the verdict is the product. A
            # full disk must never turn a REAL fallback into an "unreachable" alarm —
            # that would relabel a silent degrade as an outage and send the operator
            # looking in the wrong place. So a save failure downgrades to exit 2 only
            # when there was nothing else to report; a verdict of 1 survives it and
            # says so. (Cost of not saving: the next run re-alarms off a stale
            # baseline. A duplicate alert is strictly better than a missed one.)
            print(f"WARNING: could not persist the baseline to {args.state}: {exc}")
            if verdict == 0:
                print("No fallback to report, but the next run cannot alarm on growth — treating as operational.")
                return 2
            print("Keeping the FALLBACK verdict; the next run may re-alarm from a stale baseline.")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
