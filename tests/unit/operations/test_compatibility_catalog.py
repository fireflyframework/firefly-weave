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

"""Compatibility classifies exact retained requirements without executing integrations."""

import copy
import json
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.operations.compatibility_catalog import classify_requirement


def requirement(artifact, *, releases=None, worker_ids=None, pins=None, connector_ids=None):
    activation = Activation(
        id=uuid4(),
        revision=1,
        name="test",
        connector_execution_pins=pins or [],
        request=ActivationRequest(
            version_id=uuid4(),
            artifact_digest=artifact.digest,
            scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
            worker_release_ids=worker_ids or {},
            connector_release_ids=connector_ids or {},
        ),
    )
    return {
        "artifact": json.loads(artifact.to_bytes()),
        "activation": activation.model_dump(mode="json"),
        "releases": [r.model_dump(mode="json", by_alias=True) for r in releases or []],
        "state": {"status": "waiting", "admission_policy": "classified-v1"},
        "state_bytes": 200,
    }


@pytest.fixture
def pure():
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "test", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {"id": "copy", "kind": "transform", "value": {"ref": "/input"}},
            ],
            "output": {"ref": "/steps/copy/output"},
        },
    }
    result = compile_source(json.dumps(document), format="json", catalog=CatalogSnapshot.empty())
    assert result.ok
    return requirement(result.artifact)


@pytest.mark.parametrize("kind", ["run", "activation"])
def test_supported_pure_workflow_needs_no_online_worker(pure, kind):
    assert classify_requirement(kind, pure, ConnectorRegistry()) is None


@pytest.mark.parametrize("field", ["irVersion", "apiVersion"])
def test_unsupported_ir_is_not_legacy_policy(pure, field):
    pure["artifact"]["executable"][field] = "unknown"
    pure["state"]["unavailable"] = True
    assert classify_requirement("run", pure, ConnectorRegistry()) == "ir_unsupported"


def test_integrity_and_activation_digest_precede_legacy_policy(pure):
    pure["state"]["unavailable"] = True
    pure["artifact"]["digest"] = "f" * 64
    assert classify_requirement("run", pure, ConnectorRegistry()) == "artifact_invalid"
    pure["artifact"]["digest"] = pure["activation"]["request"]["artifact_digest"]
    pure["activation"]["request"]["artifact_digest"] = "e" * 64
    assert classify_requirement("run", pure, ConnectorRegistry()) == "artifact_invalid"


def test_known_legacy_and_unknown_policy_are_distinct(pure):
    pure["state"]["unavailable"] = True
    assert classify_requirement("run", pure, ConnectorRegistry()) == "legacy_policy_blocked"
    pure["state"].pop("unavailable")
    pure["state"].pop("admission_policy")
    assert classify_requirement("run", pure, ConnectorRegistry()) is None
    pure["state"]["admission_policy"] = "classified-v99"
    assert classify_requirement("run", pure, ConnectorRegistry()) == "policy_mismatch"


def test_unknown_explicit_worker_protocol_is_not_capacity_absence(pure):
    pure["worker_protocol"] = "weave/worker-v99"
    assert classify_requirement("run", pure, ConnectorRegistry()) == "worker_protocol_unsupported"


def test_actual_oversized_state_is_controlled_capacity_finding(pure):
    pure["state_bytes"] = 32 * 1024 * 1024 + 1
    assert classify_requirement("run", pure, ConnectorRegistry()) == "operational_capacity_blocked"


def worker_requirement(fixture):
    capability = fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    release = WorkerRelease(id=fixture["release_id"], image_digest="sha256:" + "1" * 64, capabilities=[capability])
    return requirement(fixture["artifact"], releases=[release], worker_ids={"echo": release.id})


def test_exact_worker_release_capability_and_missing_inventory(worker_runtime_fixture):
    payload = worker_requirement(worker_runtime_fixture)
    registry = ConnectorRegistry()
    assert classify_requirement("run", payload, registry) is None
    missing = copy.deepcopy(payload)
    del missing["releases"]
    assert classify_requirement("run", missing, registry) == "inventory_incomplete"
    missing["releases"] = []
    assert classify_requirement("run", missing, registry) == "action_unavailable"
    payload["releases"][0]["capabilities"][0]["timeoutSeconds"] += 1
    assert classify_requirement("run", payload, registry) == "action_unavailable"


def test_worker_shape_has_no_invented_protocol_field(worker_runtime_fixture):
    payload = worker_requirement(worker_runtime_fixture)["releases"][0]
    assert classify_requirement("worker", payload, ConnectorRegistry()) is None
    payload["protocol_version"] = "not-a-real-worker-dto-field"
    assert classify_requirement("worker", payload, ConnectorRegistry()) == "worker_protocol_unsupported"


@pytest.mark.parametrize("payload", [{}, {"artifact": []}, {"artifact": {"executable": []}}])
def test_malformed_inventory_returns_safe_codes(payload):
    assert classify_requirement("run", payload, ConnectorRegistry()) == "artifact_invalid"
    assert classify_requirement("other", payload, ConnectorRegistry()) == "inventory_incomplete"


class NeverExecute:
    async def execute(self, *args):
        raise AssertionError("Compatibility must not execute a connector")

    async def test_connection(self, *args):
        raise AssertionError("Compatibility must not contact a connector")


@pytest.fixture(params=["v1", "v2"])
def connector_requirement(request):
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.definitions import load_definition
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
    from firefly_weave.contracts.workers import ConnectorExecutionPin

    descriptor = HTTP_DESCRIPTOR if request.param == "v1" else HTTP_PROFILE_DESCRIPTOR
    manifest = descriptor.manifest.value
    reference = "weave-http@" + manifest["metadata"]["version"]
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "fetch", "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": reference,
                "action": "read",
                "config": {"method": "GET", "path": "/", "statuses": [200]},
            },
            "connection": {"connector": reference},
            "sideEffect": "read_only",
            "timeoutSeconds": 20,
            "inputSchema": manifest["spec"]["actions"]["read"]["inputSchema"],
            "outputSchema": manifest["spec"]["actions"]["read"]["outputSchema"],
        },
    }
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "flow", "version": "1.0.0"},
        "spec": {
            "connections": {"http": {"connector": reference}},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {"id": "fetch", "kind": "action", "uses": "fetch@1.0.0", "connection": "http", "with": {"literal": {}}}
            ],
            "output": {"ref": "/steps/fetch/output"},
        },
    }
    registry = ConnectorRegistry()
    registry.register_descriptor(descriptor, NeverExecute())
    catalog = CatalogSnapshot.from_definitions(
        [load_definition(manifest), load_definition(action)], adapters=[manifest["spec"]["adapter"]]
    )
    result = compile_source(json.dumps(workflow), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    action_digest = next(d["digest"] for d in result.artifact.executable["dependencies"] if d["kind"] == "Action")
    binding = descriptor.bindings[0]
    capability = descriptor.capabilities[0]
    cap_digest = (
        CatalogSnapshot.from_definitions([], tasks=[capability])
        .resolve("TaskCapability", binding.task_reference)
        .digest
    )
    release = WorkerRelease(
        id=uuid4(),
        image_digest="sha256:" + "1" * 64,
        capabilities=list(descriptor.capabilities),
        connector_bindings=list(descriptor.bindings),
    )
    version_id = uuid4()
    pin = ConnectorExecutionPin(
        **binding.model_dump(),
        action_digest=action_digest,
        capability_digest=cap_digest,
        task_type=capability.task_type,
        task_version=capability.task_version,
        connector_version_id=version_id,
        release_id=release.id,
    )
    return requirement(
        result.artifact, releases=[release], pins=[pin], connector_ids={version_id: release.id}
    ), registry


def test_both_http_descriptor_generations_remain_supported(connector_requirement):
    payload, registry = connector_requirement
    assert classify_requirement("activation", payload, registry) is None
    assert classify_requirement("worker", payload["releases"][0], registry) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_digest", "f" * 64),
        ("connector_digest", "f" * 64),
        ("capability_digest", "f" * 64),
        ("implementation_version", "99.0.0"),
        ("task_version", "99.0.0"),
        ("task_reference", "other@1.0.0"),
        ("action", "write"),
        ("connector_version_id", "00000000-0000-4000-8000-000000000001"),
        ("release_id", "00000000-0000-4000-8000-000000000002"),
    ],
)
def test_every_connector_execution_pin_is_compared(connector_requirement, field, value):
    payload, registry = connector_requirement
    payload["activation"]["connector_execution_pins"][0][field] = value
    assert classify_requirement("activation", payload, registry) == "connector_unsupported"


def test_connector_retirement_and_release_drift_block(connector_requirement):
    payload, registry = connector_requirement
    drift = copy.deepcopy(payload)
    drift["releases"][0]["capabilities"][0]["timeoutSeconds"] += 1
    assert classify_requirement("activation", drift, registry) == "connector_unsupported"
    registry.retire(payload["activation"]["connector_execution_pins"][0]["adapter"])
    assert classify_requirement("activation", payload, registry) == "connector_unsupported"


def test_duplicate_pin_is_not_collapsed(connector_requirement):
    payload, registry = connector_requirement
    payload["activation"]["connector_execution_pins"].append(
        copy.deepcopy(payload["activation"]["connector_execution_pins"][0])
    )
    assert classify_requirement("activation", payload, registry) == "connector_unsupported"


def test_provider_exact_package_schema_and_adapter_pins(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from firefly_weave.compiler.catalog import FrozenDocument
    from firefly_weave.connectors.packages import PackageMetadata
    from firefly_weave.contracts.providers import ProviderSource

    metadata = PackageMetadata(
        FrozenDocument.from_value(json.loads(Path("tests/fixtures/e2-provider/connector.json").read_text()))
    )
    package = SimpleNamespace(metadata=metadata)
    registry = ConnectorRegistry()
    monkeypatch.setattr(registry, "_packages", (package,))
    registry.register_descriptor(metadata.descriptor, NeverExecute())
    distribution, version, provider, digest, adapter_version = registry.provider_identity(package)
    source = ProviderSource(
        name="source",
        provider=provider,
        package=distribution,
        package_version=version,
        adapter_version=adapter_version,
        schema_digest=digest,
        connection_revision_id=uuid4(),
        policy={},
        kind="run",
        activation_id=uuid4(),
        id=uuid4(),
        binding_id=uuid4(),
        principal_id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
    ).model_dump(mode="json", by_alias=True)
    assert classify_requirement("provider", source, registry) is None
    for field, value in [
        ("package", "other"),
        ("package_version", "99.0.0"),
        ("provider", "other"),
        ("schema_digest", "f" * 64),
        ("adapter_version", "99.0.0"),
    ]:
        changed = source | {field: value}
        assert classify_requirement("provider", changed, registry) == "provider_requirement_unsupported"
    registry.retire(metadata.model.manifest.spec.adapter)
    assert classify_requirement("provider", source, registry) == "provider_requirement_unsupported"


def test_secret_schema_legacy_policy_does_not_hide_invalid_artifact(pure):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.compiler.canonical import canonical_digest

    executable = pure["artifact"]["executable"]
    # An unused but valid classified schema is enough to require recorded admission provenance.
    schema = {"type": "string", "x-secret": True}
    executable["schemas"][canonical_digest(schema)] = schema
    pure["artifact"]["digest"] = canonical_digest(executable)
    pure["activation"]["request"]["artifact_digest"] = pure["artifact"]["digest"]
    import_artifact(pure["artifact"])
    pure["state"].pop("admission_policy")
    assert classify_requirement("run", pure, ConnectorRegistry()) == "legacy_policy_blocked"
    pure["state"]["admission_policy"] = "classified-v1"
    assert classify_requirement("run", pure, ConnectorRegistry()) is None


def test_corrupt_release_metadata_and_duplicates_fail_closed(worker_runtime_fixture):
    payload = worker_requirement(worker_runtime_fixture)
    payload["releases"].append(copy.deepcopy(payload["releases"][0]))
    assert classify_requirement("run", payload, ConnectorRegistry()) == "action_unavailable"
    payload["releases"].pop()
    payload["releases"][0]["credential_capabilities"] = ["unoffered@1.0.0"]
    assert classify_requirement("run", payload, ConnectorRegistry()) == "action_unavailable"


def test_comparison_ir_is_advertised_and_classified_without_a_worker():
    from firefly_weave.contracts.public import Capabilities

    source = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "comparison", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [],
            "output": {
                "op": {"name": "contains", "args": [{"literal": [1]}, {"literal": 1}]},
            },
        },
    }
    compiled = compile_source(source, format="object", catalog=CatalogSnapshot.empty())
    assert compiled.ok
    assert compiled.artifact.executable["irVersion"] == "weave/ir-v1alpha3"
    capabilities = Capabilities(limits={}, schemas=[], connectors=[])
    assert compiled.artifact.executable["irVersion"] in capabilities.ir_versions
    assert classify_requirement("run", requirement(compiled.artifact), ConnectorRegistry()) is None


@pytest.fixture
def text():
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greeting", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [],
            "output": {"op": {"name": "join", "args": [{"literal": ["a", "b"]}, {"literal": ", "}]}},
        },
    }
    result = compile_source(json.dumps(document), format="json", catalog=CatalogSnapshot.empty())
    assert result.ok
    return requirement(result.artifact)


@pytest.mark.parametrize("kind", ["run", "activation"])
def test_language_features_are_supported_only_where_the_platform_runs_them(text, kind):
    assert classify_requirement(kind, text, ConnectorRegistry(), features=("text.concat", "text.join")) is None
    assert classify_requirement(kind, text, ConnectorRegistry(), features=("text.concat",)) == "ir_unsupported"
    assert classify_requirement(kind, text, ConnectorRegistry(), features=()) == "ir_unsupported"


def test_an_unknown_feature_name_is_ir_unsupported_even_with_a_valid_digest(text):
    executable = text["artifact"]["executable"]
    executable["features"] = ["flow.tryCatch"]
    text["artifact"]["digest"] = canonical_digest(executable)
    text["activation"]["request"]["artifact_digest"] = text["artifact"]["digest"]
    assert classify_requirement("run", text, ConnectorRegistry(), features=("text.join",)) == "ir_unsupported"


def test_the_platform_classifies_against_the_features_it_advertises(text):
    assert classify_requirement("activation", text, ConnectorRegistry()) is None
    executable = text["artifact"]["executable"]
    executable["features"] = ["flow.forEach"]
    text["artifact"]["digest"] = canonical_digest(executable)
    text["activation"]["request"]["artifact_digest"] = text["artifact"]["digest"]
    assert classify_requirement("activation", text, ConnectorRegistry()) == "ir_unsupported"
