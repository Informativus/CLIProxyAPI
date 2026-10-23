# Copperside CPA operational baseline

## Purpose

This repository is Copperside's controlled source and release record for the
CLIProxyAPI (CPA) service. It keeps the source revision, local fixes, and
production release process reproducible. OAuth credentials, client API keys,
and the runtime configuration are operational data and are never committed.

## Current baseline

- Upstream: CLIProxyAPI `v8.0.40`
  (`d946c4fba6fc9a5cfbca1f3d85184d62ad2fb683`).
- Copperside patch: `854551452e38484f0ca360e927071976337ac7de`
  (`fix(codex): retry duplex bootstrap overloads`).
- Release build identity: `8.0.40-cpa-bootstrap.1`. The release PR and build
  artifact record the exact source commit and binary checksum.
- Previous runtime, retained for rollback:
  `local/cpa-main-cli-proxy-api:216b55b1` (`8.0.36-cpa-bootstrap.1`).

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
- the container has a 1 GiB memory limit and `GOMEMLIMIT=768MiB`, which leaves
  room for allocations outside the Go heap and prevents the earlier cgroup
  memory-limit crashes;
- legacy Responses `reasoning_effort` is mapped to `reasoning.effort`; an
  explicitly supplied canonical value takes precedence;
- file logging is enabled with a 20 MiB total limit and three retained error
  files. Debug and full request logging are disabled.

A `429 Usage credits are required for this model` response is an upstream
account-credit refusal. It is distinct from a transient capacity rejection and
is deliberately surfaced to the client.

## Required release process

Dokploy's raw compose declaration is the deployment source of truth. The
previous declaration drift was corrected on 2026-10-23: it now retains the
Copperside image, `pull_policy: never`, the memory limit, and the Go soft memory
budget. Keep those settings when changing the image for a release.

Every future upgrade must use this sequence:

1. Start from a named upstream tag and rebase or port the Copperside patch.
2. Run the existing Go checks and build a CGO-enabled Linux amd64 binary using
   `.github/workflows/copperside-release.yml`. Build a Copperside-owned
   immutable image and record its source commit and checksum.
3. Change Dokploy's declared image to that immutable image. Never replace a
   running container by hand while leaving the declaration unchanged.
4. Retain the prior image reference and persistent volume for rollback.
5. Wait for container health, verify the management endpoint, then run a new
   real request through the intended client and model route.
6. Record the upstream tag, Copperside commit, image digest, deployment time,
   and verification result in the release merge request.

This process applies to the main CPA only. The separate `/fd/` instance has its
own release lifecycle and must not be changed as part of this upgrade.

The upstream contributor-only path restriction and automatic PR retargeting
apply only to the upstream repository. Copperside releases target this fork's
`main` through a reviewed PR, including the complete named upstream release.
The release workflow gates the deployed API, executor, config, translator, and
SDK paths. The initial full v8.0.40 test run also found failures in optional
Home heartbeat and Git-store corruption cases; the build artifact records an
isolated upstream run of those packages for comparison. Those integrations are
not enabled on the main CPA instance.
