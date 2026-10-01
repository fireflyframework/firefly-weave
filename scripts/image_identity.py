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

"""Verify copied release inputs and record actual installed image package members."""

import hashlib
import importlib
import json
import sys
from pathlib import Path

root = Path("/opt/weave")
release = json.loads((root / "release.json").read_text())
for name, expected in release["inputs"].items():
    path = root / name
    if path.name != name or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("Image input identity mismatch")
if "--verify-only" not in sys.argv:
    package = Path(importlib.import_module("firefly_weave").__file__).parent
    sources = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*.py"))
    }
    with (root / "build.json").open("x") as stream:
        json.dump({"wheel_sha256": release["wheel_sha256"], "sources": sources}, stream, sort_keys=True)
