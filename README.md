# qaEngineer

The fleet's **QA Engineer** as a protoAgent plugin bundle
([ADR 0078](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0078-fleet-pr-review-qa-tier.md),
Phase D) — Quinn's production review guards around protoAgent's adversarial panel.

```
python -m server plugin install https://github.com/protoLabsAI/qaEngineer
```

## What it assembles

| Member | Pin | Role |
|---|---|---|
| `workflows` (builtin) | core | the recipe engine the review panels run on |
| [github-plugin](https://github.com/protoLabsAI/github-plugin) | v0.2.0 | the verdict surface — formal Review API tools with CI-terminal + self-review guards inside the tools |
| [pr-reviewer-plugin](https://github.com/protoLabsAI/pr-reviewer-plugin) | v0.5.0 | the machinery — webhook chokepoint, structural trigger, panel dispatch, approve-on-green sweep, telemetry + eval, protoPatch structural finder, existing-thread awareness |

Persona: [`SOUL.md`](./SOUL.md) (Vera — verdict system, three-layer verification,
80% bar, self-restriction), also inlined in the manifest's `archetype.soul` so the
new-agent picker seeds it.

## Stages (operator-gated, data-earned)

1. **Shadow** *(shipped default)* — `shadow_mode: true`: every verdict posts as a
   COMMENT review on the same PRs Quinn and CodeRabbit review. The plugin's eval
   (`GET /api/plugins/pr-reviewer/eval` + three-way rows) accumulates the comparison.
2. **Second formal layer** — flip `shadow_mode: false` per repo: real
   PASS/WARN/FAIL verdicts alongside the incumbent; approve-on-green promotion stays
   with the incumbent (`promotion_owner: false`).
3. **Per-repo handover** — where the data shows this layer dominating on catch-rate
   + noise, grant `promotion_owner: true` (and retire the incumbent seat) for that
   repo. No program-level cutover date.

## Operator setup

1. Install the bundle; enable `workflows, github, pr-reviewer`.
2. Set `pr_reviewer.repos` (the managed allowlist), the GitHub webhook
   (`https://<host>/plugins/pr-reviewer/webhook`, content type JSON, secret →
   `pr_reviewer.webhook_secret` in Settings), and `github.write: true`.
3. Requirements on the host: `git`, `gh` (authenticated as the reviewer identity —
   NOT an identity that authors PRs in the managed repos), `clawpatch`
   (`npm i -g @protolabsai/protopatch`), gateway credentials.

_Bundle CI validates the manifest on every push._

## Deploying Vera (the reference host)

This repo doubles as Vera's image source: `Dockerfile` = stock protoAgent +
node/`clawpatch` + the bundle members baked at their manifest pins +
`deploy/vera.langgraph-config.yaml` (seed, not force) + `SOUL.md`. Published as
`ghcr.io/protolabsai/vera:latest` on every main push. She runs **headless**
(`PROTOAGENT_UI: none`): no console — the tailnet port serves the token-gated
operator API (eval, manual dispatch) and A2A; GitHub webhooks arrive via the
fleet's `hooks.proto-labs.ai` cloudflared path route. The compose service +
ingress live in homelab-iac (`stacks/vera/`). Secrets (all env, Infisical):
`OPENAI_API_KEY`, `A2A_AUTH_TOKEN` (`VERA_API_KEY`), `GH_TOKEN` (protoReview),
`PR_REVIEWER_WEBHOOK_SECRET`.
