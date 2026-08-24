#!/usr/bin/env bash
# Runner + alerter for Vera's watchdogs — health, drift, fallback, oauth, prune
# (qaEngineer#37 shipped the first two; the model-lane pair arrived with the move to a
# native Claude subscription).
#
# WHY THIS FILE EXISTS AT ALL, AND WHY IT LIVES HERE:
#
# The first two shipped in qaEngineer/scripts/ with docstrings saying "run from the ava
# fleet cron" — and then nothing ever ran them. The review-health alarm was written
# precisely because 24 PRs merged with no review and the only record was an inbox
# nobody read; leaving that alarm unscheduled reproduced the same failure one level up.
#
# It runs the INSTALLED copies in ~/.local/bin, NOT qaEngineer/scripts/*.py: the repo
# working tree is also the deploy source, so a branch switch deletes/reverts the
# in-repo script and the guard stops silently. That is not hypothetical — the same
# trap cost config-drift.sh ~5h of silent failure on 2026-08-10, and this repo sat on
# a feature branch for part of 2026-08-17 during the 0.137.1 bump. A watchdog must not
# live inside the thing it watches. Refresh these copies when the repo version changes:
#
#     install -m 644 ~/dev/qaEngineer/scripts/vera_api.py             ~/.local/bin/vera_api.py
#     install -m 755 ~/dev/qaEngineer/scripts/prune_checkout_cache.py ~/.local/bin/vera-prune-cache.py
#     install -m 755 ~/dev/qaEngineer/scripts/check_review_health.py   ~/.local/bin/vera-review-health.py
#     install -m 755 ~/dev/qaEngineer/scripts/check_card_drift.py      ~/.local/bin/vera-card-drift.py
#     install -m 755 ~/dev/qaEngineer/scripts/check_model_fallback.py  ~/.local/bin/vera-model-fallback.py
#     install -m 755 ~/dev/qaEngineer/scripts/check_oauth_health.py    ~/.local/bin/vera-oauth-health.py
#     install -m 755 ~/dev/qaEngineer/scripts/vera-watchdog.sh         ~/.local/bin/vera-watchdog.sh
#
# THIS FILE is the canonical copy (it was unversioned until 2026-08-21 — the alerting
# half of the guard living nowhere but one box's ~/.local/bin is its own quiet risk).
# The installed copy is still what cron runs, for the branch-trap reason above.
#
# A failing check must be LOUD. Exit 1 (a real verdict) and exit 2 (couldn't reach the
# agent) are deliberately different alerts — the scripts draw that line on purpose, and
# collapsing it would let an outage read as a clean gate.
#
# Usage:  vera-watchdog.sh health|drift|fallback|oauth|prune [extra args passed to the check]
# Exit:   passes the underlying check's exit code through (0 ok, 1 verdict, 2 unreachable)

set -uo pipefail

REPO="${VERA_WATCHDOG_REPO:-$HOME/dev/qaEngineer}"
BIN="$HOME/.local/bin"
MODE="${1:-}"; shift || true   # remaining args pass through to the underlying check

# The alert path must not depend on a credential that can silently expire. On
# 2026-08-21 ava's `infisical login` session had lapsed and two health runs fell through
# to the no-secrets branch: the checks ran, passed, and would have alerted to NOTHING if
# they had failed — the guard disarmed with no signal, which is the exact failure class
# these watchdogs exist to catch, one level up. So: env first (infisical run, when a
# session is good), then a local 0600 file that no session can invalidate.
#
#     install -d -m 700 ~/.config/vera
#     printf 'DISCORD_WEBHOOK_ALERTS=%s\n' "$(infisical secrets get DISCORD_WEBHOOK_ALERTS --plain ...)" \
#       > ~/.config/vera/alerts.env && chmod 600 ~/.config/vera/alerts.env
#
# Regenerate it if the webhook is ever rotated — this is a cached copy, not the source.
ALERT_ENV="${VERA_ALERT_ENV:-$HOME/.config/vera/alerts.env}"

alert() {  # alert <title> <body>
    local hook="${DISCORD_WEBHOOK_ALERTS:-}"
    if [ -z "$hook" ] && [ -r "$ALERT_ENV" ]; then
        # shellcheck disable=SC1090
        . "$ALERT_ENV"
        hook="${DISCORD_WEBHOOK_ALERTS:-}"
    fi
    if [ -z "$hook" ]; then
        echo "vera-watchdog: no DISCORD_WEBHOOK_ALERTS (env or $ALERT_ENV) — NOT alerted" >&2
        return
    fi
    local body
    body="$(printf '**%s** on %s\n```\n%s\n```' "$1" "$(hostname)" "$2")"
    python3 -c 'import json,sys;print(json.dumps({"content":sys.argv[1][:1900]}))' "$body" \
      | curl -sf -X POST -H 'Content-Type: application/json' -d @- "$hook" >/dev/null \
      && echo "vera-watchdog: alerted Discord" >&2 \
      || echo "vera-watchdog: Discord post FAILED" >&2
}

case "$MODE" in
  health)
    out="$("$BIN/vera-review-health.py" --container vera "$@" 2>&1)"; rc=$?
    ;;
  drift)
    # The seed is the reference this check compares the live card against, so it must
    # come from the repo — but reading the WORKING TREE would reintroduce the branch
    # trap through the back door (a feature branch's seed is not the deployed one).
    # Read it out of the committed main ref instead: always the deployed truth, and
    # immune to whatever the tree is currently checked out at.
    seed="$(mktemp)"; trap 'rm -f "$seed"' EXIT
    if ! git -C "$REPO" show main:deploy/vera.langgraph-config.yaml >"$seed" 2>/dev/null; then
        out="cannot read deploy/vera.langgraph-config.yaml from main in $REPO"; rc=2
    else
        out="$("$BIN/vera-card-drift.py" --seed "$seed" "$@" 2>&1)"; rc=$?
    fi
    ;;
  fallback)
    # Did Vera silently answer from her FALLBACK model? The check reads
    # `routing.fallback_models` from her LIVE config and counts only gateway traffic
    # requesting those, so it stays correct whichever lane is primary — an earlier
    # version hardcoded "any protoAgent-UA gateway traffic is a fallback", which was
    # true only while the primary was a native-OAuth subscription bypassing the gateway,
    # and inverted the moment she moved back to a gateway primary.
    #
    # Why infer at all: on cores before 0.145.0 protoAgent emitted NOTHING on failover
    # (langchain's ModelFallbackMiddleware swallows the primary's exception without so
    # much as a log line; filed as protoAgent#2956, fixed there). Once the RUNNING
    # instance is on 0.145.0+ and its `model.fallback` event is seen firing, retire this
    # inference and subscribe to the event instead.
    out="$("$BIN/vera-model-fallback.py" --container vera "$@" 2>&1)"; rc=$?
    ;;
  oauth)
    # Is the subscription credential itself still sound (signed in, refreshable, and
    # coherent with model.name)? The CAUSE side of the same failure — an idle agent can
    # outlive its refresh token with no traffic to reveal it.
    out="$("$BIN/vera-oauth-health.py" --container vera "$@" 2>&1)"; rc=$?
    ;;
  prune)
    # STOPGAP, not a watchdog: bound the checkout cache, because pr-reviewer's own
    # CheckoutCache.prune() is defined, documented, unit-tested and never called
    # (pr-reviewer-plugin#87). It reached 43 GiB / 1248 entries against its own
    # 5 GiB / 50-entry caps and took ava to 92% disk. Delete this mode when #87 ships.
    # Runs with --apply here; the underlying script dry-runs by default.
    out="$("$BIN/vera-prune-cache.py" --container vera --apply "$@" 2>&1)"; rc=$?
    ;;
  *)
    echo "usage: $(basename "$0") health|drift|fallback|oauth|prune" >&2; exit 64 ;;
esac

echo "$out"
case "$rc" in
  0) ;;
  1) alert "vera $MODE check FAILED" "$out" ;;
  *) alert "vera $MODE check UNREACHABLE (exit $rc)" "$out" ;;
esac
exit "$rc"
