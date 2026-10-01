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

"""Release image builds use one validated context and exact immutable output IDs."""

import importlib.util
from pathlib import Path

import pytest


def test_build_requires_owned_context_before_any_docker_call(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/build_release_images.py"
    spec = importlib.util.spec_from_file_location("build_release_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        module.build(tmp_path, tmp_path / "images", "default")
    assert calls == []
