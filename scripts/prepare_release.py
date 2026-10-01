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

"""Build once and export independent locked closures without invoking Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path

from firefly_weave.sdk.deployment import read_file, real_path, requirements_bytes, run_command

CLOSURES = {
    "base": (),
    "worker": ("worker",),
    "server": ("server",),
    "teams": ("server", "teams"),
    "kafka": ("server", "kafka"),
}


def prepare(root: Path, destination: Path) -> dict:
    root = real_path(root)
    destination = real_path(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Release destination must be new with an existing parent")
    destination.mkdir(mode=0o700)
    artifacts = destination / "artifacts"
    run_command(
        ["uv", "build", "--project", str(root), "--out-dir", str(artifacts)],
        timeout=180,
        limit=4 * 1024 * 1024,
        log_path=destination / "build.log",
    )
    wheels = list(artifacts.glob("*.whl"))
    sdists = list(artifacts.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Exactly one wheel and sdist required")
    context = destination / "images"
    context.mkdir(mode=0o700)
    inputs = {
        wheels[0].name: read_file(wheels[0], 64 * 1024 * 1024),
        "Dockerfile": read_file(root / "Dockerfile", 1024 * 1024),
        "worker.Dockerfile": read_file(root / "docker/worker.Dockerfile", 1024 * 1024),
        "image_identity.py": read_file(root / "scripts/image_identity.py", 1024 * 1024),
        "main.py": read_file(root / "examples/worker/main.py", 1024 * 1024),
        "manifest.json": read_file(root / "examples/worker/manifest.json", 1024 * 1024),
        "LICENSE": read_file(root / "LICENSE", 1024 * 1024),
        "NOTICE": read_file(root / "NOTICE", 1024 * 1024),
    }
    for name, extras in CLOSURES.items():
        command = [
            "uv",
            "export",
            "--quiet",
            "--project",
            str(root),
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
        ]
        for extra in extras:
            command.extend(["--extra", extra])
        inputs[name + "-requirements.txt"] = requirements_bytes(
            run_command(command, timeout=60, limit=4 * 1024 * 1024, log_path=destination / (name + "-export.log"))
        )
    wheel_hash = hashlib.sha256(inputs[wheels[0].name]).hexdigest()
    # The CLI is a client tool: keep its closure independent of server and worker images.
    cli_requirements = requirements_bytes(
        run_command(
            [
                "uv",
                "export",
                "--quiet",
                "--project",
                str(root),
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                "--extra",
                "client",
                "--extra",
                "openapi",
            ],
            timeout=60,
            limit=4 * 1024 * 1024,
            log_path=destination / "cli-export.log",
        )
    )
    installer = {
        "schema_version": 1,
        "version": tomllib.loads(read_file(root / "pyproject.toml", 1024 * 1024).decode())["project"]["version"],
        "wheel": wheels[0].name,
        "wheel_sha256": wheel_hash,
        "requirements": "cli-requirements.txt",
        "requirements_sha256": hashlib.sha256(cli_requirements).hexdigest(),
    }
    for name, data in {
        "cli-requirements.txt": cli_requirements,
        "cli-install.json": (json.dumps(installer, sort_keys=True, indent=2) + "\n").encode(),
        "install.sh": read_file(root / "install.sh", 1024 * 1024),
    }.items():
        with (artifacts / name).open("xb") as stream:
            stream.write(data)
    with (artifacts / "SHA256SUMS").open("x") as stream:
        # uv adds a private .gitignore to its output; publish only named release assets.
        names = (wheels[0].name, sdists[0].name, "cli-install.json", "cli-requirements.txt", "install.sh")
        for name in sorted(names):
            digest = hashlib.sha256(read_file(artifacts / name, 64 * 1024 * 1024)).hexdigest()
            stream.write(f"{digest}  {name}\n")
    value = {
        "complete": True,
        "wheel": wheels[0].name,
        "wheel_sha256": wheel_hash,
        "sdist": sdists[0].name,
        "sdist_sha256": hashlib.sha256(read_file(sdists[0], 64 * 1024 * 1024)).hexdigest(),
        "inputs": {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
    }
    for name, data in inputs.items():
        with (context / name).open("xb") as stream:
            stream.write(data)
    with (context / ".dockerignore").open("xb") as stream:
        stream.write(read_file(root / ".dockerignore", 1024 * 1024))
    for target in (context / "release.json", destination / "release.json"):
        with target.open("x") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.root, args.output)
    except Exception:
        parser.exit(1, "Release preparation failed; check prerequisites and the new output directory.\n")
    print(json.dumps({"wheel_sha256": result["wheel_sha256"], "sdist_sha256": result["sdist_sha256"]}))
