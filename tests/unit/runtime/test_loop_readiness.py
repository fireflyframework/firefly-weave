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

"""Activation readiness runs every IR node kind this platform executes, loops included, and nothing else."""

import asyncio
from types import SimpleNamespace
from typing import get_args
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from test_loop_runtime import compiled, each

from firefly_weave.compiler import ir
from firefly_weave.contracts import language_features
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.workers.service import WorkerService

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
LOOP = {"id": "double", "kind": "transform", "value": {"ref": "/item"}}


def admit(executable: dict) -> list:
    service = WorkerService(MagicMock(), MagicMock())
    request = ActivationRequest(version_id=uuid4(), artifact_digest="a" * 64, scope=SCOPE)

    async def run() -> list:
        return await service.admit(
            MagicMock(),
            SCOPE,
            request,
            SimpleNamespace(executable=executable),
            capability="release.activate",
            context=MagicMock(),
            tx=MagicMock(),
        )

    return asyncio.run(run())


def loop_executable() -> dict:
    return dict(
        compiled([each("each", [LOOP], {"ref": "/steps/double/output"})], {"ref": "/steps/each/output"}).executable
    )


def test_activation_accepts_a_workflow_with_a_loop():
    executable = loop_executable()
    assert "forEach" in {node["kind"] for node in executable["graph"]["nodes"]}
    assert admit(executable) == []


def test_activation_refuses_a_node_kind_this_platform_does_not_execute():
    executable = loop_executable()
    executable["graph"] = {**executable["graph"], "nodes": [*executable["graph"]["nodes"], {"kind": "callWorkflow"}]}
    with pytest.raises(CatalogError) as refused:
        admit(executable)
    assert refused.value.code == "WV-READINESS"


def test_activation_refuses_loops_on_a_platform_that_does_not_advertise_them(monkeypatch):
    executable = loop_executable()
    monkeypatch.setattr(
        language_features,
        "ADVERTISED_FEATURES",
        tuple(name for name in language_features.ADVERTISED_FEATURES if name != "flow.forEach"),
    )
    with pytest.raises(CatalogError) as refused:
        admit(executable)
    assert refused.value.code == "WV-READINESS"


def test_executable_node_kinds_are_every_ir_node_kind_whose_feature_is_advertised():
    kinds = {
        kind
        for model in get_args(get_args(ir.IRNode.__value__)[0])
        for kind in get_args(model.model_fields["kind"].annotation)
    }
    unadvertised = {
        kind
        for kind, feature in language_features.KIND_FEATURES.items()
        if feature not in language_features.ADVERTISED_FEATURES
    }
    assert ir.executable_node_kinds() == kinds - unadvertised
    assert ir.executable_node_kinds(()) == kinds - set(language_features.KIND_FEATURES)
