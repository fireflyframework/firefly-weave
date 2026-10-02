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

"""Reject structurally invalid macOS bundles before collecting installers."""

import runpy
import subprocess
from pathlib import Path

import pytest


def test_invalid_bundle_seal_stops_collection(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "desktop/scripts/verify_macos_bundle.py"
    app = tmp_path / "Studio.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/host").write_bytes(b"executable")
    calls = []

    def reject(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", reject)
    verify = runpy.run_path(str(script))["verify"]
    with pytest.raises(subprocess.CalledProcessError):
        verify(app)
    assert calls[0][:5] == ["codesign", "--verify", "--deep", "--strict", "--verbose=4"]


def test_missing_bundle_is_rejected(tmp_path):
    script = Path(__file__).resolve().parents[2] / "desktop/scripts/verify_macos_bundle.py"
    with pytest.raises(RuntimeError, match="Missing macOS application"):
        runpy.run_path(str(script))["verify"](tmp_path / "absent.app")


def test_mounted_installer_is_detached_when_seal_fails(tmp_path, monkeypatch):
    import plistlib

    script = Path(__file__).resolve().parents[2] / "desktop/scripts/verify_macos_bundle.py"
    app = tmp_path / "Studio.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "codesign":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=plistlib.dumps(
                {"system-entities": [{"dev-entry": "/dev/test-owned", "mount-point": str(tmp_path)}]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(script))["verify_dmg"](tmp_path / "Studio.dmg")
    assert calls[0][:4] == ["hdiutil", "attach", "-readonly", "-nobrowse"]
    assert calls[-1] == ["hdiutil", "detach", "/dev/test-owned"]
