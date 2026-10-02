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

"""Fail packaging when nested executable or outer macOS bundle seals are invalid."""

import argparse
import plistlib
import subprocess
from pathlib import Path


def verify(app: Path) -> None:
    binaries = app / "Contents/MacOS"
    if not binaries.is_dir():
        raise RuntimeError("Missing macOS application bundle")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app)], check=True)
    for binary in sorted(binaries.iterdir()):
        if binary.is_file():
            subprocess.run(["codesign", "--verify", "--strict", "--verbose=4", str(binary)], check=True)
    if not (app / "Contents/_CodeSignature/CodeResources").is_file():
        raise RuntimeError("Missing outer macOS resource seal")


def verify_dmg(dmg: Path) -> None:
    result = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(dmg)],
        check=True,
        capture_output=True,
    )
    entities = plistlib.loads(result.stdout)["system-entities"]
    mounts = [Path(entity["mount-point"]) for entity in entities if "mount-point" in entity]
    device = next(entity["dev-entry"] for entity in entities if "dev-entry" in entity)
    try:
        apps = [app for mount in mounts for app in mount.glob("*.app")]
        if len(apps) != 1:
            raise RuntimeError("Installer must contain exactly one application")
        verify(apps[0])
    finally:
        subprocess.run(["hdiutil", "detach", device], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    app = parser.parse_args().app
    verify_dmg(app) if app.suffix == ".dmg" else verify(app)


if __name__ == "__main__":
    main()
