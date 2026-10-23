# Copperside CPA operational baseline

## Purpose

This repository is Copperside's controlled source and release record for the
CLIProxyAPI (CPA) service. It keeps the source revision, local fixes, and
production release process reproducible. OAuth credentials, client API keys,
and the runtime configuration are operational data and are never committed.

## Current baseline

- Upstream: CLIProxyAPI `v8.0.36`.
- Copperside patch: `854551452e38484f0ca360e927071976337ac7de`
  (`fix(codex): retry duplex bootstrap overloads`).
- Current runtime build identity: `8.0.36-cpa-bootstrap.1`, built from commit
  `216b55b1fec03d2f9cb2cb4ba2e8139a5890e4fb`.

The repository uses the upstream MIT-licensed source as its baseline. Local
commits must remain small, reviewed, and rebaseable onto an upstream release.

## What CPA does

CPA is the gateway between Codex-compatible clients and upstream providers.
It accepts OpenAI-compatible requests, selects an eligible OAuth credential,
opens the upstream Codex WebSocket session, and translates the streamed result
back to the client.

The production instance runs as a Dokploy-managed Docker service. Persistent
state is mounted at `/data` inside the service and includes the active
configuration and OAuth credentials. Never delete, recreate, or prune that
volume during an upgrade.

The Management Center is only the UI. Its static page updates independently of
the CPA binary, so a new Management Center version does not update the proxy.

## Copperside reliability change

Codex can accept the WebSocket handshake and then reject the first response for
transient capacity reasons. Before the patch, that rejection could reach the
client after the session became visible, so clients showed `Reconnecting` even
though another CPA credential could have been tried.

The patch holds only the harmless initial WebSocket frames. If the upstream
returns a recognised capacity rejection before visible output, CPA releases the
attempt to its credential conductor without disconnecting the client session.
Once content or an unknown frame appears, the stream follows the normal path.
It does not choose another model and does not hide non-capacity failures.

The current live settings complement the code change:

- `stream-bootstrap-timeout: 20s` gives the upstream enough time to start;
- `transient-error-cooldown-seconds: 8` temporarily avoids a credential that
  just returned a transient failure;
- model fallback is intentionally disabled. A request stays on its chosen
  model.

A `429 Usage credits are required for this model` response is an upstream
account-credit refusal. It is distinct from a transient capacity rejection and
is deliberately surfaced to the client.

## Required release process

There is currently a deployment-drift risk: Dokploy's compose declaration
references the upstream `v8.0.36` image, while the running container uses a
locally built image containing the Copperside patch. A routine Dokploy deploy
can therefore remove the patch.

Every future upgrade must use this sequence:

1. Start from a named upstream tag and rebase or port the Copperside patch.
2. Build a Copperside-owned immutable image and record its source commit.
3. Change Dokploy's declared image to that immutable image. Never replace a
   running container by hand while leaving the declaration unchanged.
4. Retain the prior image reference and persistent volume for rollback.
5. Wait for container health, verify the management endpoint, then run a new
   real request through the intended client and model route.
6. Record the upstream tag, Copperside commit, image digest, deployment time,
   and verification result in the release merge request.

Until the declared image is brought in sync with the running custom image,
avoid ordinary Dokploy redeploys of CPA.
