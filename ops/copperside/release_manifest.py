"""Emit the install contract only after release checks and image packaging pass."""
import hashlib
import json
import os
from pathlib import Path

archive = Path("dist/cpa-image.tar.gz")
version = Path("ops/copperside/version").read_text().strip()
manifest = {
    "schema": 1,
    "version": version,
    "upstream": "v8.0.44",
    "commit": os.environ["GITHUB_SHA"],
    "image": os.environ["CPA_IMAGE"],
    "image_id": os.environ["CPA_IMAGE_ID"],
    "asset": archive.name,
    "sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
    "size": archive.stat().st_size,
}
Path("dist/cpa-update.json").write_text(json.dumps(manifest, indent=2) + "\n")
