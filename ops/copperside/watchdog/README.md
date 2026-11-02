# Main CPA memory recovery

On 2026-11-02 the main CPA remained running but unhealthy at 1020–1022 MiB
of its 1 GiB limit. `memory.events` reported 3,172,742 limit hits and zero
OOM kills. Both management HTML and unauthenticated API requests timed out,
including directly on the runtime host. Restarting only the main container
restored HTML 200, API 401 and approximately 256 MiB usage. Usage aggregation
was already disabled and queue retention was 60 seconds. The precise retained
allocation is not established: no heap profile was captured before restart.

This independent watchdog provides recovery, not a claimed leak fix. Every
30 seconds it checks only the exact main Compose service. After a three-minute
startup grace, four unhealthy samples or seven samples above 95% of the memory
limit trigger a restart. Active requests can disconnect during recovery.
It never drops host caches, deletes CPA data, changes credentials, restarts
FD, or revives an intentionally stopped container. Metrics and recovery
attempts persist separately. There is a 30-minute cooldown and at most three
attempts per rolling day; ambiguous restart responses count as attempts.
An active/unknown updater job inhibits recovery. Missing/unreadable updater
state also inhibits it. Operator deployments must stop this watchdog first,
then start it after verification to avoid racing external deployments.

Docker socket access is privileged; this container has no network or published
ports. The existing pinned updater image supplies Python and the Docker CLI.
Its image must already exist on the codex host. No image is pulled implicitly.

## Delivery

Use task branch -> reviewed PR -> merged current main, then the release train
`cpa-main@codex` with ship ref `fork/main`. Copy this directory from the clean
merged checkout into `/home/codex/cpa-main-watchdog/<source-sha>` on codex.
From that directory, with `SOURCE_SHA` set to that merged revision:

```sh
# Build and run recovery for the main Copperside Codex gateway only.
docker build --label org.opencontainers.image.revision="$SOURCE_SHA" \
  -t "local/cpa-main-watchdog:$SOURCE_SHA" .
docker compose -f compose.yml config --quiet
docker compose -f compose.yml up -d
docker compose -f compose.yml logs --tail 10
```

Read `/state/latest.json` and `/state/recovery.json` in the watchdog container
for current observations and bounded recovery history. To disable automatic
recovery, create `/state/disabled`, or stop the watchdog with `docker compose
stop watchdog`. Rollback removes/stops only this watchdog, without deleting
its state volume or touching the main CPA. `--observe-once` collects real
metrics without restarting anything; use a separate state directory so it
does not contend with the running supervisor lock.

Verify the exact source revision and all three Atlas labels with Docker
inspect, healthy CPA, management HTML 200 and unauthenticated API 401.
Those probes do not establish successful model inference or long-term freedom
from memory growth. Inspect the labels in the next fresh Atlas inventory.
