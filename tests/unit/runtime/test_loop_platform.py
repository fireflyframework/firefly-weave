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

"""This platform runs loops; a platform that does not list the loop feature still refuses them."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import UnsupportedIR
from firefly_weave.contracts.language import language_manifest, supported_step_kinds
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

LOOP = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "label-items", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "array", "items": {"type": "string"}},
        "outputSchema": {"type": "array"},
        "steps": [
            {
                "id": "label",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "body": {"steps": [], "output": {"ref": "/item"}},
            }
        ],
        "output": {"ref": "/steps/label/output"},
    },
}


@pytest.fixture
def artifact():
    result = compile_source(LOOP, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return result.artifact


def test_this_platform_lists_the_loop_feature_and_the_loop_step(artifact):
    assert "flow.forEach" in language_manifest().features
    assert "forEach" in supported_step_kinds()
    assert import_artifact(artifact.to_bytes()).digest == artifact.digest


def test_a_platform_without_the_loop_feature_still_refuses_a_loop(artifact):
    with pytest.raises(UnsupportedIR) as refused:
        import_artifact(artifact.to_bytes(), features=["text.concat", "text.join"])
    assert refused.value.missing == ("flow.forEach",)


def test_the_kernel_runs_a_loop(artifact):
    started = RuntimeEvent(id=uuid4(), type="started", timestamp=datetime(2026, 10, 13, tzinfo=UTC), sequence=1)
    result = transition(RunState(input=["a", "b"]), started, artifact)
    assert (result.state.status, result.state.output) == ("succeeded", ["a", "b"])
    assert result.state.incidents == {}
