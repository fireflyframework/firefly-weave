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

from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.agentic import (
    AGENTIC_DESCRIPTOR,
    AgenticConnectionAdapter,
    action_definition,
    input_schema,
    keyless,
    output_schema,
    task_capability,
)
from firefly_weave.contracts.connectors import ConnectionInvalid, ConnectionRequest, ConnectionTestResult
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.llm import LLMProfile


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


@pytest.mark.parametrize("endpoint", ["https://API.openai.com:443/v1", "https://api.openai.com/v1/"])
@pytest.mark.parametrize("destination", ["https://api.openai.com", "https://API.openai.com:443"])
def test_provider_connection_matches_the_canonical_https_origin(endpoint, destination):
    from uuid import uuid4

    request = ConnectionRequest(
        name="provider",
        connector_version_id=uuid4(),
        config={"provider": "openai-chat", "endpoint": endpoint, "secretSlot": "apiKey"},
        secretRef={"apiKey": "provider-key"},
        allowed_destinations=(destination,),
    )
    AGENTIC_DESCRIPTOR.validate_connection(request)


# Measured on main before Ollama support; option A changes no catalog document.
CONNECTOR_DIGEST = "7dfc18419ba042e7dff2a15198373c13da3e1a5e83a78976c422993f57a937e7"
ACTION_DIGEST = "cd1ab7add02d438b419cb206fdb7164c027fe5c5a5b4b3029af2402ce60ebbf0"
CAPABILITY_DIGEST = "d47d20b4b3f1d56567f7eef1ff5665aa5606136f8979f8b9bf33a5e2f2b7c4bd"
OLLAMA = "http://ollama:11434"


def test_catalog_documents_keep_their_digests():
    assert AGENTIC_DESCRIPTOR.manifest.digest == CONNECTOR_DIGEST
    action = load_definition(action_definition()).model_dump(by_alias=True)
    assert FrozenDocument.from_value(action).digest == ACTION_DIGEST
    capability = task_capability().model_dump(mode="json", by_alias=True)
    assert FrozenDocument.from_value(capability).digest == CAPABILITY_DIGEST


def test_typed_call_profile_defaults_to_eight_requests():
    profile = LLMProfile.model_validate(
        {"provider": "openai-chat", "model": "qwen3:4b", "options": {"max_tokens": 1024}, "outputSchema": {}}
    )
    assert profile.max_calls == 8


def model_entries(credentials="none", origin=OLLAMA, networks=("10.246.21.0/24",)):
    entry = po.PrivateOrigin(origin=origin, purpose="model", networks=networks, credentials=credentials)
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])


def ollama_request(endpoint=OLLAMA + "/v1", handle="no-credential", destinations=(OLLAMA,)):
    return ConnectionRequest(
        name="ollama-local",
        connector_version_id=uuid4(),
        config={"provider": "openai-chat", "endpoint": endpoint, "secretSlot": "apiKey"},
        secretRef={"apiKey": handle},
        allowed_destinations=destinations,
    )


def issues(request):
    with pytest.raises(ConnectionInvalid) as refused:
        AGENTIC_DESCRIPTOR.validate_connection(request)
    return {(issue.path, issue.message) for issue in refused.value.issues}


def test_plain_http_needs_a_model_entry_that_sends_no_credentials():
    with po.installed(po.PrivateOrigins.empty()):
        assert issues(ollama_request()) == {
            (
                "/config/endpoint",
                "This address is not approved for plain HTTP. "
                "A platform operator must add it to the private-origin policy.",
            ),
            ("/secretRef/apiKey", "no-credential is reserved for approved local model endpoints."),
        }
        assert not keyless({"endpoint": OLLAMA + "/v1"}, {"apiKey": "no-credential"})
    with po.installed(model_entries()):
        AGENTIC_DESCRIPTOR.validate_connection(ollama_request())
        assert keyless({"endpoint": OLLAMA + "/v1"}, {"apiKey": "no-credential"})


def test_plain_http_never_carries_a_credential():
    with po.installed(model_entries()):
        assert {path for path, _ in issues(ollama_request(handle="openai-api-key"))} == {"/secretRef/apiKey"}
        assert not keyless({"endpoint": OLLAMA + "/v1"}, {"apiKey": "openai-api-key"})


def test_the_gateway_loopback_entry_is_never_a_connection_endpoint():
    gateway = "http://127.0.0.1:8090"
    with po.installed(model_entries(credentials="loopback", origin=gateway, networks=("127.0.0.1/32",))):
        found = issues(ollama_request(endpoint=gateway + "/v1", destinations=(gateway,)))
    assert "/config/endpoint" in {path for path, _ in found}


def test_an_unlisted_plain_http_origin_is_refused_when_another_is_listed():
    other = "http://ollama-elsewhere.test:11434"
    with po.installed(model_entries()):
        found = issues(ollama_request(endpoint=other + "/v1", destinations=(other,)))
    assert "/config/endpoint" in {path for path, _ in found}


def test_no_credential_is_reserved_for_approved_local_endpoints():
    request = ConnectionRequest(
        name="openai",
        connector_version_id=uuid4(),
        config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "no-credential"},
        allowed_destinations=("https://api.openai.com",),
    )
    with po.installed(po.PrivateOrigins.empty()):
        assert {path for path, _ in issues(request)} == {"/secretRef/apiKey"}


async def test_connection_test_without_a_gateway_reports_failure():
    result = await AgenticConnectionAdapter().test_connection(SimpleNamespace())
    assert result == ConnectionTestResult(ok=False, code="failed")


async def test_connection_test_delegates_to_the_gateway_tester():
    seen = []

    async def tester(connection):
        seen.append(connection)
        return ConnectionTestResult(ok=True)

    connection = SimpleNamespace()
    assert (await AgenticConnectionAdapter(tester).test_connection(connection)).ok is True
    assert seen == [connection]
