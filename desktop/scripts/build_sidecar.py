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

"""Freeze the trusted host and compiled UI for the current native target only."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def signing_arguments(identity: str | None, root: Path) -> list[str]:
    if not identity:
        return []
    return [
        "--codesign-identity",
        identity,
        "--osx-entitlements-file",
        str(root / "desktop/src-tauri/entitlements.plist"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    info = subprocess.check_output(["rustc", "-vV"], text=True)
    target = next(line.removeprefix("host: ") for line in info.splitlines() if line.startswith("host: "))
    if args.target is not None and args.target != target:
        raise SystemExit("Build the Python sidecar on the matching OS and architecture; cross-freezing is unsupported")
    assets = root / "studio/dist/studio/browser"
    if not (assets / "index.html").is_file():
        raise SystemExit("Build the Studio frontend first: cd studio && npm ci && npm run build")
    work = root / "desktop/work"
    work.mkdir(exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        "weave-studio-host",
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work),
        "--paths",
        str(root / "src"),
        "--collect-all",
        "pyfly",
        "--collect-data",
        "rfc3987_syntax",
        "--collect-data",
        "firefly_weave",
        "--copy-metadata",
        "firefly-weave",
        "--copy-metadata",
        "pyfly",
        "--copy-metadata",
        "keyring",
        "--add-data",
        str(assets) + os.pathsep + "studio-assets",
        str(root / "src/firefly_weave/studio/desktop.py"),
    ]
    identity = os.environ.get("APPLE_SIGNING_IDENTITY")
    if sys.platform == "darwin" and identity:
        command[3:3] = signing_arguments(identity, root)
    subprocess.run(command, check=True, cwd=root)
    extension = ".exe" if sys.platform == "win32" else ""
    source = work / "dist" / ("weave-studio-host" + extension)
    destination = root / "desktop/src-tauri/binaries" / ("weave-studio-host-" + target + extension)
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (work / "sidecar-manifest.json").write_text(
        json.dumps({"target": target, "sha256": digest, "filename": destination.name}, indent=2) + "\n"
    )
    print("Frozen Studio host: " + destination.name)


if __name__ == "__main__":
    main()
