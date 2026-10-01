# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Build independent release images from one hash-verified prepared wheel context."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from firefly_weave.sdk.deployment import read_file, real_path, run_command, wheel_licenses


def build(release: Path, output: Path, context: str) -> dict:
    if context != "colima-weave-tests":
        raise ValueError("Release builds require the explicitly owned Docker context")
    release, output = real_path(release), real_path(output)
    metadata = json.loads(read_file(release / "release.json", 1024 * 1024))
    if metadata.get("complete") is not True:
        raise ValueError("Completed release preparation required")
    directory = release / "images"
    names = {entry.name for entry in directory.iterdir()}
    expected = {
        metadata["wheel"],
        "Dockerfile",
        "worker.Dockerfile",
        "image_identity.py",
        "main.py",
        "manifest.json",
        "LICENSE",
        "NOTICE",
        "base-requirements.txt",
        "worker-requirements.txt",
        "server-requirements.txt",
        "teams-requirements.txt",
        "kafka-requirements.txt",
    }
    if names != expected | {"release.json", ".dockerignore"} or set(metadata["inputs"]) != expected:
        raise ValueError("Prepared container context differs from the product allowlist")
    if json.loads(read_file(directory / "release.json", 1024 * 1024)) != metadata:
        raise ValueError("Container release identity differs")
    for name, digest in metadata["inputs"].items():
        if hashlib.sha256(read_file(directory / name, 64 * 1024 * 1024)).hexdigest() != digest:
            raise ValueError("Prepared input changed")
    wheel_licenses(read_file(directory / metadata["wheel"], 64 * 1024 * 1024), metadata["wheel"])
    docker = ["docker", "--context", context]
    endpoint = json.loads(run_command(docker + ["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise ValueError("Local Docker context required")
    output.mkdir(mode=0o700)
    images = {}
    for name, target, filename in (
        ("server", "server", "Dockerfile"),
        ("worker", "base", "worker.Dockerfile"),
        ("teams", "teams-server", "Dockerfile"),
        ("kafka", "kafka-server", "Dockerfile"),
    ):
        receipt = output / (name + ".iid")
        run_command(
            docker
            + [
                "build",
                "--progress=plain",
                "--target",
                target,
                "--iidfile",
                str(receipt),
                "-f",
                str(directory / filename),
                str(directory),
            ],
            timeout=1200,
            limit=4 * 1024 * 1024,
            log_path=output / (name + ".log"),
        )
        image = read_file(receipt, 100).decode().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Build did not return an exact immutable image ID")
        details = json.loads(run_command(docker + ["image", "inspect", image]))[0]
        if details["Id"] != image or details["Config"]["User"] != "65532:65532":
            raise ValueError("Image identity or nonroot policy mismatch")
        images[name] = image
    result = {"complete": True, "wheel_sha256": metadata["wheel_sha256"], "images": images}
    with os.fdopen(os.open(output / "images.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    try:
        result = build(args.release, args.output, args.context)
    except Exception:
        parser.exit(1, "Image build failed; inspect bounded private logs. Existing images were retained.\n")
    print(json.dumps(result))
