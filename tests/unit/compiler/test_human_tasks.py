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

"""Human feature admission is explicit and legacy executable identity is stable."""

import copy
import json

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot


def definition():
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "human", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "review",
                    "kind": "humanTask",
                    "assignment": "reviewers",
                    "title": {"literal": "Review"},
                    "context": {"literal": {}},
                    "formSchema": {"type": "object"},
                }
            ],
            "output": {"ref": "/steps/review/output"},
        },
    }


def compile(document):
    return compile_source(document, format="object", catalog=CatalogSnapshot.empty())


def test_human_feature_uses_new_ir_and_cannot_masquerade_as_old():
    result = compile(definition())
    assert result.ok
    assert result.artifact.executable["irVersion"] == "weave/ir-v1alpha2"
    envelope = json.loads(result.artifact.to_bytes())
    envelope["executable"]["irVersion"] = "weave/ir-v1alpha1"
    envelope["digest"] = canonical_digest(envelope["executable"])
    with pytest.raises(ValueError):
        import_artifact(envelope)


@pytest.mark.parametrize(
    "changes",
    [
        {"decisions": []},
        {"decisions": ["approve", "approve"]},
        {"dueSeconds": 0},
        {"expirySeconds": None},
        {"formSchema": {"type": "not-a-type"}},
        {"title": {"literal": 7}},
        {"context": {"literal": "context"}},
    ],
)
def test_invalid_human_contracts_are_rejected(changes):
    document = definition()
    document["spec"]["steps"][0].update(changes)
    assert not compile(document).ok


def test_legacy_without_human_stays_on_old_ir_without_new_execution_fields():
    document = definition()
    document["spec"]["steps"] = [{"id": "review", "kind": "transform", "value": {"literal": 7}}]
    first = compile(document)
    assert first.ok and first.artifact.executable["irVersion"] == "weave/ir-v1alpha1"
    assert "features" not in first.artifact.executable
    assert import_artifact(copy.deepcopy(json.loads(first.artifact.to_bytes()))).digest == first.artifact.digest
