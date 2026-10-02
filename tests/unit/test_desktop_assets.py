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

"""Frozen asset lookup must not evaluate source-tree paths on native targets."""

from pathlib import Path

from firefly_weave.studio import desktop


def test_frozen_assets_do_not_evaluate_shallow_source_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(desktop, "__file__", str(Path(tmp_path.anchor) / "desktop.py"))
    assert desktop.packaged_assets() == tmp_path / "studio-assets"
