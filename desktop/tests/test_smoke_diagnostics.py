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

"""Keep bounded offline-only startup evidence when native CI freezing fails."""

import runpy
from pathlib import Path

import pytest


async def test_smoke_retains_bounded_offline_failure_stderr(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/smoke_sidecar.py"
    binary = tmp_path / "broken-host"
    binary.write_text("#!/bin/sh\nprintf 'offline-import-error' >&2\nexit 1\n")
    binary.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    check = runpy.run_path(str(script))["check"]
    with pytest.raises(RuntimeError, match="before readiness"):
        await check(binary)
    assert (tmp_path / "desktop/work/frozen-startup-error.txt").read_text() == "offline-import-error"
