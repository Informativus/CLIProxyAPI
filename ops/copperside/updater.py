"""Authenticated, persistent updater for one Dokploy-managed CPA instance.

The controller is independent of CPA so replacing the proxy cannot lose the job.
Only checked release assets from the configured fork can be installed.
"""
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bcrypt
import yaml

os.umask(0o077)
CONFIG = json.loads(Path("/run/cpa-updater.json").read_text())
STATE = Path("/state")
STATE.mkdir(exist_ok=True)
PROCESS_LOCK = (STATE / "controller.lock").open("w")
fcntl.flock(PROCESS_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
LOCK = threading.RLock()
ACTIVE = {"queued", "downloading", "backing_up", "deploying", "verifying", "unknown"}
JOB_FILE = STATE / "job.json"
JOB = json.loads(JOB_FILE.read_text()) if JOB_FILE.exists() else {"status": "idle"}
CACHE = None
CACHE_AT = 0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, body=None, headers=None, limit=2 * 1024 * 1024):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "Copperside-CPA-Updater", **(headers or {})})
    with OPENER.open(req, timeout=30) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Response exceeds limit")
        return json.loads(data) if data else None


def dokploy(path, body=None):
    return request(CONFIG["dokploy_url"].rstrip("/") + "/api/" + path, body,
                   {"x-api-key": CONFIG["dokploy_key"]})


def declaration():
    return dokploy("compose.one?composeId=" + CONFIG["compose_id"])


def docker(*args, timeout=30):
    return subprocess.check_output(["docker", *args], timeout=timeout,
                                   stderr=subprocess.PIPE).decode()


def runtime():
    return json.loads(docker("inspect", CONFIG["container"]))[0]


def save_job(**changes):
    with LOCK:
        JOB.update(changes, updated_at=int(time.time()))
        temporary = JOB_FILE.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(JOB, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(JOB_FILE)


def version_order(value):
    # Copperside revisions remain ordered without treating a suffix as upstream.
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-cpa(?:-bootstrap)?\.(\d+))?", value)
    if not match:
        raise ValueError("Unsupported version format")
    return tuple(int(part or 0) for part in match.groups())


def release(refresh=False):
    global CACHE, CACHE_AT
    with LOCK:
        if CACHE and not refresh and time.monotonic() - CACHE_AT < 300:
            return CACHE
        repo = CONFIG["release_repository"]
        info = request("https://api.github.com/repos/" + repo + "/releases/latest")
        if info.get("draft") or info.get("prerelease"):
            raise ValueError("Release is not installable")
        assets = {asset["name"]: asset for asset in info["assets"]}
        prefix = "https://github.com/" + repo + "/releases/download/"
        manifest_url = assets["cpa-update.json"]["browser_download_url"]
        if not manifest_url.startswith(prefix):
            raise ValueError("Unexpected release origin")
        manifest = request(manifest_url, limit=16384)
        version_order(manifest["version"])
        if (manifest["schema"] != 1
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["image_id"])
                or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])
                or manifest["image"] != "local/cpa-main-cli-proxy-api:"
                   + manifest["version"] + "-" + manifest["commit"][:12]
                or manifest["asset"] != "cpa-image.tar.gz"
                or not 0 < manifest["size"] <= 512 * 1024 * 1024
                or assets[manifest["asset"]]["size"] != manifest["size"]):
            raise ValueError("Invalid release manifest")
        url = assets[manifest["asset"]]["browser_download_url"]
        if not url.startswith(prefix):
            raise ValueError("Unexpected archive origin")
        CACHE = {**manifest, "tag": info["tag_name"], "url": url}
        CACHE_AT = time.monotonic()
        return CACHE


def current_version(current):
    return current["Config"].get("Labels", {}).get("org.opencontainers.image.version", "")


def healthy(current, image_id):
    return (current["Image"] == image_id and current["State"].get("Running")
            and current["State"].get("Health", {}).get("Status") == "healthy")


def reconcile():
    # After controller interruption, observe first; never replay a deployment.
    if JOB["status"] != "unknown":
        return
    current, source = runtime(), declaration()
    declared_image = yaml.safe_load(source["composeFile"])["services"]["cli-proxy-api"]["image"]
    if source["composeStatus"] == "done":
        if declared_image == JOB.get("image") and healthy(current, JOB.get("image_id")):
            save_job(status="succeeded", error=None)
        elif (declared_image == JOB.get("previous_image")
              and healthy(current, JOB.get("previous_image_id"))):
            save_job(status="failed", error="Update did not replace the previous version.")


def status(refresh=False):
    reconcile()
    current = runtime()
    result = {"supported": True, "current_version": current_version(current),
              "job": dict(JOB), "available": False}
    try:
        candidate = release(refresh)
        result.update(latest_version=candidate["version"], release=candidate["tag"])
        result["available"] = (JOB["status"] not in ACTIVE
                               and version_order(candidate["version"]) > version_order(result["current_version"]))
    except Exception:
        result["check_error"] = "Cannot read an installable Copperside release. Try checking again."
    return result


def run_update(candidate):
    stage = "downloading"
    mutation_started = False
    try:
        save_job(status=stage)
        archive = STATE / "download.tar.gz"
        digest = hashlib.sha256()
        downloaded = 0
        with OPENER.open(candidate["url"], timeout=60) as response, archive.open("wb") as output:
            while block := response.read(1024 * 1024):
                downloaded += len(block)
                if downloaded > candidate["size"]:
                    raise ValueError("Archive too large")
                digest.update(block)
                output.write(block)
        if downloaded != candidate["size"] or not hmac.compare_digest(digest.hexdigest(), candidate["sha256"]):
            raise ValueError("Archive checksum mismatch")
        docker("load", "-i", str(archive), timeout=180)
        archive.unlink()
        image = json.loads(docker("image", "inspect", candidate["image"]))[0]
        if (image["Id"] != candidate["image_id"]
                or image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != candidate["commit"]
                or current_version(image) != candidate["version"]):
            raise ValueError("Loaded image identity mismatch")
        stage = "backing_up"
        save_job(status=stage)
        source, before = declaration(), runtime()
        compose = yaml.safe_load(source["composeFile"])
        service = compose["services"]["cli-proxy-api"]
        previous = service["image"]
        if (source["sourceType"] != "raw" or source["composeStatus"] != "done"
                or previous != before["Config"]["Image"]
                or not healthy(before, before["Image"])
                or service.get("mem_limit") != "1g" or service.get("memswap_limit") != "1g"
                or service.get("environment", {}).get("GOMEMLIMIT") != "768MiB"
                or service.get("pull_policy") != "never"):
            raise ValueError("Deployment baseline changed")
        if version_order(candidate["version"]) <= version_order(current_version(before)):
            raise ValueError("Release is no longer newer")
        backup = STATE / JOB["id"]
        backup.mkdir()
        (backup / "compose-before.yaml").write_text(source["composeFile"])
        config = Path("/cpa-data/config.yaml").read_bytes()
        (backup / "config-before.yaml").write_bytes(config)
        with tarfile.open(backup / "auths-before.tar.gz", "w:gz") as output:
            output.add("/cpa-data/auths", arcname="auths")
        # Replace precisely one image scalar; all other declared settings stay byte-for-byte.
        pattern = r"(?m)^(\s+image:\s*)" + re.escape(previous) + r"\s*$"
        updated, count = re.subn(pattern, lambda match: match[1] + candidate["image"], source["composeFile"])
        expected = yaml.safe_load(source["composeFile"])
        expected["services"]["cli-proxy-api"]["image"] = candidate["image"]
        if count != 1 or yaml.safe_load(updated) != expected:
            raise ValueError("Cannot safely replace declared image")
        if declaration()["composeFile"] != source["composeFile"]:
            raise ValueError("Concurrent declaration change")
        if runtime()["Id"] != before["Id"] or Path("/cpa-data/config.yaml").read_bytes() != config:
            raise ValueError("Concurrent runtime change")
        save_job(previous_image=previous, previous_image_id=before["Image"])
        stage = "deploying"
        save_job(status=stage)
        mutation_started = True
        dokploy("compose.update", {"composeId": CONFIG["compose_id"], "composeFile": updated})
        if declaration()["composeFile"] != updated:
            raise ValueError("Declaration readback mismatch")
        dokploy("compose.deploy", {"composeId": CONFIG["compose_id"],
                                  "title": "CPA " + candidate["version"] + " from management panel",
                                  "description": "Verified Copperside release " + candidate["commit"]})
        stage = "verifying"
        save_job(status=stage)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            time.sleep(3)
            current, source = runtime(), declaration()
            if source["composeStatus"] == "error":
                raise ValueError("Dokploy deployment failed")
            if (source["composeStatus"] == "done" and healthy(current, candidate["image_id"])
                    and current_version(current) == candidate["version"]):
                if Path("/cpa-data/config.yaml").read_bytes() != config:
                    raise ValueError("Runtime configuration changed during upgrade")
                save_job(status="succeeded", error=None)
                return
        raise TimeoutError("Deployment verification timed out")
    except Exception as error:
        # Once deployment may have started, retain the lockout until reconciled.
        print(json.dumps({"event": "update_failed", "stage": stage, "type": type(error).__name__}), flush=True)
        save_job(status="unknown" if mutation_started else "failed",
                 error="Update could not be confirmed during " + stage
                       + (". Deployment will not be repeated automatically." if mutation_started else ". The running CPA was not changed."))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # Do not log credentials, query strings, or request bodies.

    def reply(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authenticated(self):
        origin = self.headers.get("Origin")
        if origin and origin != CONFIG["public_origin"]:
            return False
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return False
        key = authorization[7:].encode()
        if not 0 < len(key) <= 72:
            return False
        remote = yaml.safe_load(Path("/cpa-data/config.yaml").read_text())["management"]
        expected = remote.get("secret-key", "").encode()
        if not expected or not remote.get("allow-remote"):
            return False
        return bcrypt.checkpw(key, expected) if expected.startswith((b"$2a$", b"$2b$", b"$2y$")) else hmac.compare_digest(key, expected)

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, update):
        try:
            if self.path not in ("/v8/management/server/update", "/v8/management/server/update?refresh=1"):
                self.reply(404, {"error": "Not found"})
                return
            if not self.authenticated():
                self.reply(401, {"error": "Invalid management key"})
                return
            if not update:
                self.reply(200, status(self.path.endswith("?refresh=1")))
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.reply(415, {"error": "JSON required"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                self.reply(400, {"error": "Invalid request length"})
                return
            body = json.loads(self.rfile.read(length))
            with LOCK:
                if JOB["status"] in ACTIVE:
                    self.reply(409, {"error": "An update is already in progress", "job": dict(JOB)})
                    return
                candidate = release(refresh=True)
                if body != {"release": candidate["tag"]}:
                    self.reply(409, {"error": "Release changed. Check for updates again."})
                    return
                current = runtime()
                if version_order(candidate["version"]) <= version_order(current_version(current)):
                    self.reply(409, {"error": "This release is already installed"})
                    return
                JOB.clear()
                save_job(id=str(time.time_ns()), status="queued", version=candidate["version"],
                         image=candidate["image"], image_id=candidate["image_id"], error=None)
                threading.Thread(target=run_update, args=(candidate,), daemon=True).start()
                self.reply(202, {"job": dict(JOB)})
        except (ValueError, KeyError):
            self.reply(400, {"error": "Invalid request or release metadata"})
        except Exception as error:
            print(json.dumps({"event": "request_failed", "type": type(error).__name__}), flush=True)
            self.reply(503, {"error": "Update service is temporarily unavailable"})


if JOB["status"] in ACTIVE:
    save_job(status="unknown", error="Controller restarted. Checking deployment outcome.")
ThreadingHTTPServer(("0.0.0.0", 8319), Handler).serve_forever()
