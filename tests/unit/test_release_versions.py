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

"""Every release surface names the same version, so a partial bump fails the checks (acceptance spec §8.1)."""

import json
import re
import tomllib
from pathlib import Path

import pytest

import firefly_weave

ROOT = Path(__file__).resolve().parents[2]


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


CORE = tomllib.loads(text("pyproject.toml"))["project"]["version"]
_PARTS = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)a(\d+)", CORE)
assert _PARTS, CORE
ALPHA = int(_PARTS[4])
DESKTOP = f"{_PARTS[1]}.{_PARTS[2]}.{_PARTS[3]}-alpha.{ALPHA}"
NUMERIC = f"{_PARTS[1]}.{_PARTS[2]}.{ALPHA}"
WORKER = f"0.1.{ALPHA - 8}"
TAG = f"v{CORE}"
PINS = (
    re.compile(r"releases/download/(v[^/\s]+)/"),
    re.compile(r"--version (v\d+\.\d+\.\d+a\d+)"),
    re.compile(r"--branch (v\d+\.\d+\.\d+a\d+)"),
    re.compile(r"pinned \*\*(v\d+\.\d+\.\d+a\d+) alpha\*\*"),
)
PRINTED = re.compile(r"Firefly Weave (\d+\.\d+\.\d+a\d+)")
HISTORY = {"docs/operations/upgrades.md"}


def locked(relative: str, name: str) -> str:
    packages = [package for package in tomllib.loads(text(relative))["package"] if package["name"] == name]
    assert len(packages) == 1, (relative, name)
    return packages[0]["version"]


def test_core_package_lock_and_studio_bundle_name_the_core_version():
    assert firefly_weave.__version__ == CORE
    assert locked("uv.lock", "firefly-weave") == CORE
    assert 'f"firefly-weave-studio-{__version__}.zip"' in text("src/firefly_weave/studio/assets.py")


@pytest.mark.parametrize(("worker", "package"), [("agentic", "weave-agentic-worker"), ("files", "weave-files-worker")])
def test_workers_follow_the_core_version(worker, package):
    project = tomllib.loads(text(f"workers/{worker}/pyproject.toml"))["project"]
    assert project["version"] == WORKER
    pins = [dependency for dependency in project["dependencies"] if dependency.startswith("firefly-weave")]
    assert len(pins) == 1 and pins[0].endswith("==" + CORE), pins
    assert locked(f"workers/{worker}/uv.lock", package) == WORKER
    assert locked(f"workers/{worker}/uv.lock", "firefly-weave") == CORE


def test_desktop_versions_follow_the_core_version():
    config = json.loads(text("desktop/src-tauri/tauri.conf.json"))
    assert config["version"] == DESKTOP
    assert config["bundle"]["windows"]["wix"]["version"] == NUMERIC
    assert config["bundle"]["macOS"]["bundleVersion"] == NUMERIC
    assert tomllib.loads(text("desktop/src-tauri/Cargo.toml"))["package"]["version"] == DESKTOP
    assert locked("desktop/src-tauri/Cargo.lock", "firefly-weave-studio") == DESKTOP
    package = json.loads(text("desktop/package.json"))
    lock = json.loads(text("desktop/package-lock.json"))
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == DESKTOP


def test_documented_install_pins_name_the_release_tag():
    found = []
    for page in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        relative = page.relative_to(ROOT).as_posix()
        if relative in HISTORY or "superpowers" in page.parts:
            continue
        content = page.read_text(encoding="utf-8")
        found += [(relative, value) for pattern in PINS for value in pattern.findall(content)]
        found += [(relative, "v" + value) for value in PRINTED.findall(content)]
    assert found
    assert sorted({pin for pin in found if pin[1] != TAG}) == []


def test_desktop_release_notes_name_the_versions():
    notes = text("desktop/RELEASE.md")
    for phrase in (
        f"The alpha{ALPHA} product version is `{DESKTOP}`",
        f"Python `{CORE}`",
        f"tag `{TAG}`",
        f"numeric version `{NUMERIC}`",
        f"numeric `{NUMERIC}`",
    ):
        assert phrase in notes, phrase
