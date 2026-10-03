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

"""The API exposes a pure model-worker catalog without importing Agentic or provider SDKs."""

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.agentic import (
    AGENTIC_DESCRIPTOR,
    AgenticConnectionAdapter,
    action_definition,
    input_schema,
    output_schema,
    task_capability,
)
from firefly_weave.contracts.connectors import ConnectionInvalid, ConnectionRequest
from firefly_weave.contracts.definitions import load_definition


def test_worker_catalog_compiles_with_shared_schemas_and_static_server_descriptor():
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    assert "weave-agentic-provider" in registry.descriptors()
    assert not validate_schema(input_schema(), {})
    assert not validate_schema(output_schema({"type": "boolean"}), {})
    snapshot = CatalogSnapshot.from_definitions(
        [load_definition(AGENTIC_DESCRIPTOR.manifest.value)],
        tasks=[task_capability()],
        adapters=["weave-agentic-provider"],
    )
    result = compile_source(action_definition(), format="object", catalog=snapshot)
    assert result.ok, result.to_bytes()


@pytest.mark.parametrize("destinations,slot", [([], "apiKey"), (["https://api.openai.com"], "other")])
def test_provider_connection_requires_endpoint_destination_and_declared_secret_slot(destinations, slot):
    from uuid import uuid4

    request = ConnectionRequest(
        name="provider",
        connector_version_id=uuid4(),
        config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
        secretRef={slot: "provider-key"},
        allowed_destinations=tuple(destinations),
    )
    with pytest.raises(ConnectionInvalid):
        AGENTIC_DESCRIPTOR.validate_connection(request)


def test_result_schema_local_references_keep_their_scope_inside_output_envelope():
    from firefly_weave.compiler.schemas import validate_payload

    schema = output_schema({"$defs": {"answer": {"type": "boolean"}}, "$ref": "#/$defs/answer"})
    assert not validate_schema(schema, {})
    value = {
        "result": True,
        "usage": {"requests": 1, "inputTokens": 2, "outputTokens": 3},
        "provider": "openai-chat",
        "model": "fixture",
    }
    assert not validate_payload(schema, value, {})
    value["result"] = "true"
    assert validate_payload(schema, value, {})
