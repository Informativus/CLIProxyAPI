"""Bounded recovery for the main CPA only; never delete its persistent data."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

TARGET = "claude-dev-l3ht56-cli-proxy-api-1"
STATE = Path(os.environ.get("WATCHDOG_STATE", "/state"))
ACTIVE_UPDATES = {"queued", "downloading", "backing_up", "deploying", "verifying", "unknown"}


def docker(*args, timeout=8):
    return subprocess.check_output(["docker", *args], timeout=timeout,
                                   stderr=subprocess.PIPE).decode()


def emit(event, **values):
    print(json.dumps({"at": int(time.time()), "event": event, **values}), flush=True)


def inspect():
    item = json.loads(docker("inspect", TARGET))[0]
    labels = item["Config"].get("Labels") or {}
    if (item["Name"] != "/" + TARGET
            or labels.get("com.docker.compose.project") != "claude-dev-l3ht56"
            or labels.get("com.docker.compose.service") != "cli-proxy-api"):
        raise ValueError("Target identity mismatch")
    return item


def sample(item):
    state = item["State"]
    result = {"id": item["Id"], "started": state["StartedAt"],
              "status": state["Status"], "health": state.get("Health", {}).get("Status"),
              "failing": state.get("Health", {}).get("FailingStreak", 0),
              "limit": item["HostConfig"]["Memory"], "image": item["Image"]}
    if state["Status"] == "running":
        # Docker exec reads this exact container's cgroup, never a host-wide metric.
        result["memory"] = int(docker("exec", item["Id"], "cat", "/sys/fs/cgroup/memory.current"))
    return result


def update_active():
    # Missing/malformed state is fail-closed: do not race an unknown updater.
    job = json.loads(Path("/updater-state/job.json").read_text())
    status = job["status"]
    if status not in ACTIVE_UPDATES | {"idle", "succeeded", "failed"}:
        raise ValueError("Unknown updater state")
    return status in ACTIVE_UPDATES


def save(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observe-once", action="store_true", help="Read metrics; never restart")
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / "watchdog.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ledger_path = STATE / "recovery.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"attempts": []}
    unhealthy = high = 0
    identity = None
    while True:
        try:
            item = inspect()
            current = sample(item)
            active = update_active()
            now = time.time()
            started = datetime.datetime.fromisoformat(current["started"].replace("Z", "+00:00")).timestamp()
            key = (current["id"], current["started"])
            if identity != key:
                unhealthy = high = 0
                identity = key
            eligible = (current["status"] == "running" and now - started >= 180
                        and not active and not (STATE / "disabled").exists())
            unhealthy = unhealthy + 1 if eligible and current["health"] == "unhealthy" else 0
            high = high + 1 if (eligible and current["limit"] > 0
                               and current.get("memory", 0) >= current["limit"] * .95) else 0
            save(STATE / "latest.json", {**current, "at": int(now), "updater_active": active,
                                        "unhealthy_samples": unhealthy, "high_samples": high})
            if args.observe_once:
                emit("observe", **current, updater_active=active)
                return
            if unhealthy >= 4 or high >= 7:
                attempts = [t for t in ledger["attempts"] if now - t < 86400]
                reason = "unhealthy_90s" if unhealthy >= 4 else "memory_95pct_180s"
                if len(attempts) >= 3 or (attempts and now - attempts[-1] < 1800):
                    emit("recovery_rate_limited", reason=reason)
                else:
                    # Re-read immediately before mutation; never act on a replaced/stopped instance.
                    fresh = inspect()
                    if (fresh["Id"] != current["id"] or fresh["State"]["StartedAt"] != current["started"]
                            or fresh["State"]["Status"] != "running" or update_active()):
                        unhealthy = high = 0
                        continue
                    evidence = {**current, "at": int(now), "reason": reason}
                    try:
                        evidence["memory_events"] = docker("exec", current["id"], "cat", "/sys/fs/cgroup/memory.events")
                    except (subprocess.SubprocessError, ValueError):
                        evidence["memory_events"] = "unavailable"
                    # Record the attempt before issuing restart: an uncertain response is not retried.
                    ledger = {"attempts": [*attempts, now], "last": evidence}
                    save(ledger_path, ledger)
                    emit("recovery_start", **evidence)
                    docker("restart", "--timeout", "20", current["id"], timeout=35)
                    emit("restart_returned", container=TARGET)
                    unhealthy = high = 0
            elif unhealthy == 0 and high == 0:
                emit("healthy" if current["health"] == "healthy" else "observing",
                     memory=current.get("memory"), health=current["health"])
        except Exception as error:
            # Never log Docker stderr/config, which can contain deployment secrets.
            emit("observation_failed", error_type=type(error).__name__)
            unhealthy = high = 0
            if args.observe_once:
                raise SystemExit(1) from None
        time.sleep(30)


if __name__ == "__main__":
    main()
