# vera — stock protoAgent + the QA Engineer bundle, HEADLESS (ADR 0078 Phase D).
#
# Not a fork: the stock protoAgent image with the bundle's members baked in and
# Vera's seed config + persona. Runs `--ui`-less (PROTOAGENT_UI=none in the
# compose): no console — the tailnet port serves only the token-gated operator
# API (eval, manual dispatch) and A2A; GitHub webhooks arrive via cloudflared at
# hooks.proto-labs.ai path-routed to /plugins/pr-reviewer/webhook (HMAC-authed).
# Base is PINNED (not :latest) so a plugin-pin bump can't silently drag the
# protoAgent core forward on the same image roll — core and member bumps are
# decoupled. Bump this deliberately (and re-verify), keeping it in step with the
# manifest's `verified_against`. Tag format is bare semver (no `v` prefix).
FROM ghcr.io/protolabsai/protoagent:0.108.0

USER root

# node 22 + protoPatch (`clawpatch`) — the structural review engine the
# pr-reviewer plugin shells out to. The base (bookworm) ships node 18 at best;
# clawpatch needs >=22, so use nodesource. Pin the CLI so image builds are
# reproducible; bump deliberately.
ARG PROTOPATCH_VERSION=0.6.1
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@protolabsai/protopatch@${PROTOPATCH_VERSION}" \
    && clawpatch --version

# Bake the bundle members at their RELEASE TAGS (both public — no build secrets).
# The tags mirror protoagent.bundle.yaml's pins; bump both together (the manifest
# is the source of truth, this bake is its image form).
ARG GITHUB_PLUGIN_REF=v0.3.0
RUN git clone --depth 1 --branch "${GITHUB_PLUGIN_REF}" \
      https://github.com/protoLabsAI/github-plugin.git /opt/protoagent/plugins/github \
    && rm -rf /opt/protoagent/plugins/github/.git
ARG PR_REVIEWER_PLUGIN_REF=v0.17.1
RUN git clone --depth 1 --branch "${PR_REVIEWER_PLUGIN_REF}" \
      https://github.com/protoLabsAI/pr-reviewer-plugin.git /opt/protoagent/plugins/pr-reviewer \
    && rm -rf /opt/protoagent/plugins/pr-reviewer/.git

# Seed, don't force (config-as-code, the jon pattern): protoagent's first-boot
# seeder copies this to the live config once (PROTOAGENT_SEED_CONFIG), then
# operator edits persist in the config volume across image rolls. Re-seed =
# clear the volume.
COPY deploy/vera.langgraph-config.yaml /opt/vera/seed/langgraph-config.yaml
ENV PROTOAGENT_SEED_CONFIG=/opt/vera/seed/langgraph-config.yaml

# Vera's persona — two-pronged like jon's: the bundle copy serves a fresh
# instance whose live soul is absent; PROTOAGENT_SEED_SOUL seeds/heals the live
# one on boot (protoAgent#1782).
COPY SOUL.md /opt/protoagent/config/SOUL.md
COPY SOUL.md /opt/vera/seed/SOUL.md
ENV PROTOAGENT_SEED_SOUL=/opt/vera/seed/SOUL.md

USER 1001
