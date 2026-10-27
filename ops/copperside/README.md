# CPA panel updates

The main panel's authenticated `GET /v8/management/server/update` reports the
latest **installable Copperside release**, current version, availability and a
persisted job. `?refresh=1` refreshes the release catalog. `POST` with exactly
`{"release":"<tag returned by GET>"}` accepts one update, returning HTTP 202.
Concurrent, changed or already-installed releases return 409. The controller
uses the main CPA's current management-key hash, not a key embedded in the UI.
The `/fd/` instance and direct connections without this controller keep their
own lifecycle. The route requires the existing Tailnet access middleware.

The controller downloads an immutable image archive and verifies its size,
SHA-256, image ID, source commit and version against `cpa-update.json` from the
fork release. It saves config/auth and declaration backups in its private state
volume. Only the main service's image scalar changes in Dokploy's raw Compose.
Volumes, keys, resource limits and every other service stay unchanged. A fresh
declaration readback and healthy matching image are required for success.

Deployment happens outside the CPA process, so the controller can report status
during restart. A persistent file lock prevents duplicate controllers. Lost
responses and restarts never trigger an automatic redeployment. An uncertain
outcome stays `unknown` until the desired image is healthy, the configuration is
unchanged, and Dokploy is done. Investigate an unresolved
`unknown` job before manual rollback; never delete the lock or replay POST.
Backups and prior images are intentionally retained for operator recovery.

## Deployment

Build `Dockerfile.updater` from this directory into a versioned local image.
Add an `updater` service to the **existing Dokploy raw Compose**, then deploy it
through Dokploy. Do not edit the generated Docker Compose file. The service:

- publishes only `100.122.36.105:18321:8319`;
- mounts `/var/run/docker.sock` (Docker control is needed to load/inspect images);
- mounts `claude-dev-cli-proxy-data` read-only at `/cpa-data`;
- mounts a private persistent volume at `/state`;
- mounts a root-owned mode-0600 JSON configuration at `/run/cpa-updater.json`, read-only;
- has 256 MiB memory, one CPU, dropped capabilities and no-new-privileges;
- uses `pull_policy: never` and restart unless-stopped.

The private JSON configuration contains `dokploy_url`, `dokploy_key`,
`compose_id`, `container`, `release_repository` and `public_origin`. Production
targets compose `DUtByYSRUqEozFZG2XBYO`, container
`claude-dev-l3ht56-cli-proxy-api-1`, repository `Informativus/CLIProxyAPI`, origin
`https://cpa.copperside.ru`. Keep this file and state backups out of Git/logs.
The controller is privileged through Docker access and the Dokploy credential;
it must remain on the private network behind the authenticated management route.

Install `traefik-updater.yml` in the control host's existing dynamic file
provider. Configure the main CPA's panel repository to
`https://github.com/Informativus/cpa-management-teams` and publish its single-file
`management.html` release. This prevents an upstream panel refresh from removing
the update action. Do not change the separate `/fd/` panel.

## Publishing subsequent upgrades

1. Merge a named upstream tag on a task branch, retaining the Copperside retry
   patch. Update `version`, the upstream tag in the release workflow, and
   `release_manifest.py`. Every version must be new; never replace a release.
2. Open a PR to this fork's main. The Copperside workflow runs the existing
   release-path checks, builds CPA, packages an immutable image and validates
   the controller build. It records optional upstream integration failures
   separately. No release is advertised by an unmerged PR.
3. After checks and PR merge, dispatch `copperside-release.yml` on `main` with
   `publish=true`. The workflow publishes `cpa-update.json` only with the checked
   image. The panel can then install it without a shell or CI credential.
4. Confirm installation and run a fresh real inference request. Container health
   proves process availability, not provider or model acceptance.

The controller does not automatically build unreviewed upstream versions. A new
upstream version becomes installable once its fork release has passed this path.
