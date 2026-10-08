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

"""Artifacts produced from retained earlier-release sources must keep their original identities."""

import json
from pathlib import Path

import pytest

from firefly_weave.compiler.api import import_artifact


@pytest.mark.parametrize("name", ["pre-b8-onboarding", "pre-b8-connector-action"])
def test_pre_b8_artifact_pins_are_unchanged(name):
    envelope = json.loads(Path(f"tests/fixtures/compatibility/{name}.artifact.json").read_text())
    imported = import_artifact(envelope)
    assert imported.digest == envelope["digest"]
    assert imported.executable["dependencies"] == envelope["executable"]["dependencies"]
