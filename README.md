# qaEngineer

The fleet's **QA Engineer** as a protoAgent plugin bundle
([ADR 0078](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0078-fleet-pr-review-qa-tier.md),
Phase D) — an adversarial review panel that posts formal PASS / WARN / FAIL verdicts on
pull requests, with the deterministic machinery around it in code rather than in prompts.

```
python -m server plugin install https://github.com/protoLabsAI/qaEngineer
```

## What it assembles

| Member | Pin | Role |
|---|---|---|
| `workflows` (builtin) | core | the recipe engine the review panels run on |
| [github-plugin](https://github.com/protoLabsAI/github-plugin) | v0.3.0 | the verdict surface — formal Review API tools with CI-terminal + self-review guards inside the tools |
| [pr-reviewer-plugin](https://github.com/protoLabsAI/pr-reviewer-plugin) | v0.17.0 | the machinery — webhook chokepoint, structural trigger, panel dispatch, evidence grounding, convergence, approve-on-green sweep, on-demand summon, telemetry + eval |

Persona: [`SOUL.md`](./SOUL.md) (Vera — verdict system, three-layer verification, 80% bar,
self-restriction), also inlined in the manifest's `archetype.soul` so the new-agent picker
seeds it.

---

## Requirements

Four things must be true before a single review runs. Two of them fail **silently** if you
get them wrong, which is why they're first.

### 1. A GitHub App (the reviewer identity)

Reviews post as `<app>[bot]`, install per-repo, and are revocable. The plugin mints
installation tokens itself (`app_auth.py`) and refreshes them into the process env, so
every `gh`/`git` subprocess re-auths with no credential files on disk.

**Repository permissions:**

| Permission | Level | Used for |
|---|---|---|
| Pull requests | **Read & write** | post reviews, read diffs and files, dismiss our own stale blocks |
| Contents | **Read** | read files at the reviewed SHA (finders, evidence grounding), compare heads |
| Checks | **Read** | CI terminality — a blocking verdict only goes out against terminal checks |
| Issues | **Read & write** | comment replies for the summon surface |
| Members / Metadata | **Read** | resolve the commenter's permission for an admin-gated summon |

**Webhook events — get these right or features vanish with no error:**

| Event | Needed for |
|---|---|
| **Pull request** | the review triggers: `opened`, `synchronize`, `reopened`, `ready_for_review` |
| **Issue comment** | `@vera review` / `pause` / `resume` / `help` |
| **Pull request review comment** | inline thread replies (the refutation channel) |

> ⚠️ **This is the failure mode that cost us a day.** An App subscribed only to
> `pull_request` accepts the summon feature happily and then never delivers a single
> command — correct code, no event, no error anywhere. Verify with:
>
> ```
> GET /api/plugins/pr-reviewer/summon/health
> → {"missing": [], "summon_reachable": true}
> ```
>
> Subscribe only to what's above. Every other event still hits the public webhook route,
> gets HMAC-verified, and is dropped as noise.

**Webhook URL + secret:** point the App at `https://<host>/plugins/pr-reviewer/webhook`,
content type JSON, and set the same secret as `pr_reviewer.webhook_secret` (or
`PR_REVIEWER_WEBHOOK_SECRET`). **No secret configured ⇒ every delivery 403s** — fail
closed, never an open dispatch surface.

**Identity rule:** the App must not be an identity that *authors* PRs in the managed
repos. The dispatcher refuses to review its own PRs, but the cleanest guarantee is
separation.

### 2. Inference with enough context

The panel runs **five finders in parallel**, each receiving the PR diff plus file reads
plus prior-round context. In practice that's **~37k tokens per finder**.

- **Context window ≥ 64k** with real headroom. A 32k model does not fit and fails
  mid-review — we ran a backend at 32,768 *total* (prompt **plus** output) and every
  large-diff review exhausted the panel.
- **No aggressive TPM cap.** A free tier at 12,000 tokens/minute rejects a single finder
  outright.
- Routed through a gateway alias, so swapping models is a gateway edit rather than a code
  change. Model settings are host-scoped (ADR 0047).

**Reviews are not cheap.** One structural review is nine LLM steps and 5–9 minutes of
wall clock. On a hosted frontier model that's roughly $0.12–0.15 each; on local inference
it's free but occupies the box.

### 3. Host tooling

`git`, `gh`, and `clawpatch` (`npm i -g @protolabsai/protopatch`) — the structural finder
shells out to protoPatch. A missing or slow `clawpatch` **degrades** the panel to four
finders with a Gap noted; it never fails the review.

### 4. Config

Set `pr_reviewer.repos` (the managed allowlist — the gate runs *before* any GitHub call,
so an unlisted repo never triggers a lookup on your credentials) and `github.write: true`.

Everything operator-tunable reads **config first, env as fallback**, and resolves **live**
— editing `repos` or flipping a kill switch takes effect without a restart. See the
[plugin README](https://github.com/protoLabsAI/pr-reviewer-plugin#config-env-fallbacks)
for the full table; the ones you'll reach for:

| Knob | Default | Why you'd touch it |
|---|---|---|
| `PR_REVIEWER_SHADOW_MODE` | `true` | every verdict posts as a COMMENT — the safe starting posture |
| `PR_REVIEWER_PROMOTION_OWNER` | `false` | whether this seat owns approve-on-green |
| `PR_REVIEWER_REGATE` | `true` | stop *arming blocks* without demoting the whole seat — the lever when the panel emits false FAILs |
| `PR_REVIEWER_SUMMON` | `true` | the comment-command surface |
| `PR_REVIEWER_EVIDENCE_GROUNDING` | `true` | downgrade findings whose quoted code isn't in the file |

---

## Stages (operator-gated, data-earned)

1. **Shadow** *(shipped default)* — `shadow_mode: true`: every verdict posts as a COMMENT
   review alongside whatever already reviews those PRs. The plugin's eval
   (`GET /api/plugins/pr-reviewer/eval`, plus three-way comparison rows) accumulates the
   evidence.
2. **Second formal layer** — `shadow_mode: false` per repo: real PASS/WARN/FAIL verdicts,
   with approve-on-green promotion still owned by the incumbent (`promotion_owner: false`).
3. **Per-repo handover** — where the data shows this layer dominating on catch-rate and
   noise, grant `promotion_owner: true`. No program-level cutover date.

**Don't skip to hard branch protection.** Requiring the QA review to *approve* before
merge sounds like the natural end state and isn't: this panel has produced a
twice-confirmed hallucinated blocker, and the correct outcome was an adjudicated merge
past it. Gate rigidity must not outrun verdict reliability. If you want a merge-time
guard, require that a verdict **exists**, not that it approves.

## What the machinery does that the prompts can't

The model reviews; everything around the review is deterministic code. The parts worth
knowing because they change what lands on your PR:

- **In-diff confinement** — a finding on a file the PR didn't touch never reaches the verdict.
- **Evidence grounding** — a finding whose quoted code appears nowhere in the file at the
  reviewed SHA is downgraded to `uncertain` and cannot carry a FAIL. Fails open; nothing
  is ever dropped, only stripped of gating power.
- **Convergence** — from round 3, an all-minor WARN anchored entirely to lines that moved
  since the last review retires to PASS-with-notes, so a review loop has an exit.
- **Prior-finding dispositions** — a confirmed blocker/major must be accounted for
  (`fixed` / `open` / `refuted`) in the next round, or a standing block is held.
- **Fail-closed exhaustion** — a run with any failed panel step posts **nothing** and
  escalates. A partial panel never synthesizes a verdict.
- **Every guard reports its decision**, firing or declining, in telemetry.

## On-demand review

Repo admins can drive the panel from a PR comment (permission resolved server-side, never
from the payload):

| Command | Effect |
|---|---|
| `@vera review` | run the panel now — including on a head already reviewed, which is the point when you think a verdict was wrong |
| `@vera pause` / `resume` | stop/restore reviewing this PR on push; an explicit `review` still works while paused |
| `@vera help` | the verb list |

## Deploying Vera (the reference host)

This repo doubles as Vera's image source: `Dockerfile` = stock protoAgent (**pinned
base** — `protoagent:0.108.0`, in step with the manifest's `verified_against`; bump
deliberately so a member-pin bump can't drag the core forward on the same roll) +
node/`clawpatch` + the bundle members baked at their manifest pins +
`deploy/vera.langgraph-config.yaml` (seed, not force) + `SOUL.md`.

Published as `ghcr.io/protolabsai/vera:latest` on every main push; watchtower rolls in
~60s. She runs **headless** (`PROTOAGENT_UI: none`) — the tailnet port serves the
token-gated operator API (eval, manual dispatch, summon health) and A2A; GitHub webhooks
arrive via the fleet's `hooks.proto-labs.ai` cloudflared route. Compose + ingress live in
homelab-iac (`stacks/vera/`).

Secrets (all env, Infisical): `OPENAI_API_KEY`, `A2A_AUTH_TOKEN` (`VERA_API_KEY`),
`PROTOREVIEW_APP_ID`, `PROTOREVIEW_APP_PRIVATE_KEY`, `PR_REVIEWER_WEBHOOK_SECRET`.

> **The config volume is seed-once.** `deploy/vera.langgraph-config.yaml` is copied on
> *first* boot only, so a seed edit reaches fresh instances and **not** a running one.
> Apply live changes via the operator API or the config volume directly. This is why the
> operator-tunable state has env fallbacks: the compose env is re-applied on every roll,
> which keeps the config volume disposable.

_Bundle CI validates the manifest on every push_ — and asserts the seed carries Vera's A2A
card identity (non-template description + the `pr_review` skill), so a seed that regresses
to the stock template fails before it bakes. Because the config volume is seed-once, that
static check can't see a *running* instance whose live config drifted;
`scripts/check_card_drift.py` is the runtime half — point it at the (tailnet-only) card
from the ava fleet cron: `python3 scripts/check_card_drift.py` (exit 1 on drift).

## Other orgs

Nothing here is protoLabs-specific except the pins and the seed: the bundle installs into
any protoAgent host, and the App/permissions/events above are the whole contract. A
multi-tenant hosted version — one App serving many orgs, each bringing their own inference
— is a different product with different problems (per-org secrets, budget isolation, noisy
neighbours on a shared panel). Self-hosting is the path of least resistance and is what
this repo documents.
