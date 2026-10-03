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

"""Pure exact-pin checks over a factual compatibility inventory.

The caller owns scoped, bounded, complete database enumeration. This module never
queries a database, contacts a provider, resolves credentials or executes a graph.
Missing worker instances do not imply an unsupported execution contract.
"""

import json
from typing import Any
from uuid import UUID

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import COMPARISON_IR_VERSION, HUMAN_IR_VERSION, IR_VERSION
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.compatibility import FindingCode
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.contracts.workers import ConnectorBinding, WorkerRelease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import has_markers

WORKER_PROTOCOL = "weave/worker-v1"
_ERRORS = (ValueError, KeyError, TypeError, RecursionError, OverflowError, CatalogError)


def _release_inventory(payload: dict[str, Any], activation: Activation) -> dict[UUID, WorkerRelease]:
    values = payload.get("releases", [])
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("Invalid release inventory")
    releases = [WorkerRelease.model_validate_json(json.dumps(value, allow_nan=False)) for value in values]
    result = {release.id: release for release in releases}
    expected = set(activation.request.worker_release_ids.values()) | set(
        activation.request.connector_release_ids.values()
    )
    if len(result) != len(releases) or set(result) != expected:
        raise ValueError("Incomplete release inventory")
    return result


def _execution(
    executable: dict[str, Any], activation: Activation, releases: dict[UUID, WorkerRelease], registry: ConnectorRegistry
) -> FindingCode | None:
    dependencies = executable["dependencies"]
    indexed = {(d["kind"], d["reference"]): d for d in dependencies}
    actions = [d for d in dependencies if d["kind"] == "Action"]
    worker_types: set[str] = set()
    connector_actions: dict[str, Any] = {}
    try:
        for release in releases.values():
            registry.validate_release(release)
        for dependency in dependencies:
            if dependency["kind"] == "Connector":
                descriptor = registry.descriptor(dependency["document"]["spec"]["adapter"])
                if descriptor.manifest.digest != dependency["digest"]:
                    return "connector_unsupported"
        for action in actions:
            implementation = action["document"]["spec"]["implementation"]
            if implementation["kind"] == "connector":
                connector_actions[action["digest"]] = action
                continue
            task_type, version = implementation["taskType"], implementation["taskVersion"]
            worker_types.add(task_type)
            if task_type.startswith("weave-connector-"):
                return "action_unavailable"
            release = releases[activation.request.worker_release_ids[task_type]]
            reference = f"{task_type}@{version}"
            offered = CatalogSnapshot.from_definitions([], tasks=release.capabilities).resolve(
                "TaskCapability", reference
            )
            if offered is None or offered.digest != indexed[("TaskCapability", reference)]["digest"]:
                return "action_unavailable"
        if worker_types != set(activation.request.worker_release_ids):
            return "action_unavailable"
    except _ERRORS:
        return "action_unavailable" if worker_types else "connector_unsupported"

    try:
        pins = {pin.action_digest: pin for pin in activation.connector_execution_pins}
        if len(pins) != len(activation.connector_execution_pins) or set(pins) != set(connector_actions):
            return "connector_unsupported"
        used = set()
        for digest, action in connector_actions.items():
            pin = pins[digest]
            spec = action["document"]["spec"]
            implementation = spec["implementation"]
            dependency = indexed[("Connector", implementation["uses"])]
            descriptor = registry.descriptor(dependency["document"]["spec"]["adapter"])
            binding = ConnectorBinding.model_validate({key: getattr(pin, key) for key in ConnectorBinding.model_fields})
            offered = CatalogSnapshot.from_definitions([], tasks=descriptor.capabilities).resolve(
                "TaskCapability", pin.task_reference
            )
            if (
                binding not in descriptor.bindings
                or pin.connector_digest != dependency["digest"]
                or pin.adapter != dependency["document"]["spec"]["adapter"]
                or pin.implementation_version != descriptor.implementation_version
                or pin.action != implementation["action"]
                or pin.task_reference != f"{pin.task_type}@{pin.task_version}"
                or offered is None
                or offered.digest != pin.capability_digest
                or activation.request.connector_release_ids.get(pin.connector_version_id) != pin.release_id
                or spec.get("connection", {}).get("connector") != implementation["uses"]
            ):
                return "connector_unsupported"
            release = releases[pin.release_id]
            registry.validate_release(release)
            if binding not in release.connector_bindings:
                return "connector_unsupported"
            actual = CatalogSnapshot.from_definitions([], tasks=release.capabilities).resolve(
                "TaskCapability", pin.task_reference
            )
            if actual is None or actual.digest != pin.capability_digest:
                return "connector_unsupported"
            used.add(pin.connector_version_id)
        if used != set(activation.request.connector_release_ids):
            return "connector_unsupported"
    except _ERRORS:
        return "connector_unsupported"
    return None


def classify_requirement(kind: str, payload: dict[str, Any], registry: ConnectorRegistry) -> FindingCode | None:
    """Return only a controlled finding; a supported shape is not current execution authority."""
    if kind in {"run", "activation"}:
        envelope = payload.get("artifact")
        executable = envelope.get("executable") if isinstance(envelope, dict) else None
        if not isinstance(envelope, dict) or not isinstance(executable, dict):
            return "artifact_invalid"
        if (
            executable.get("irVersion") not in {IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION}
            or executable.get("apiVersion") != "weave/v1alpha1"
        ):
            return "ir_unsupported"
        try:
            artifact = import_artifact(envelope)
            activation = Activation.model_validate_json(json.dumps(payload["activation"], allow_nan=False))
            if executable.get("kind") != "Workflow" or activation.request.artifact_digest != artifact.digest:
                return "artifact_invalid"
        except _ERRORS:
            return "artifact_invalid"
        if payload.get("worker_protocol", WORKER_PROTOCOL) != WORKER_PROTOCOL:
            return "worker_protocol_unsupported"
        if "releases" not in payload and (
            activation.request.worker_release_ids or activation.request.connector_release_ids
        ):
            return "inventory_incomplete"
        try:
            releases = _release_inventory(payload, activation)
        except _ERRORS:
            return "action_unavailable"
        code = _execution(executable, activation, releases, registry)
        if code is not None:
            return code
        if kind == "run":
            state = payload.get("state")
            size = payload.get("state_bytes")
            if not isinstance(state, dict) or type(size) is not int or size < 0:
                return "inventory_incomplete"
            if state.get("admission_policy") not in (None, "classified-v1"):
                return "policy_mismatch"
            if type(state.get("unavailable", False)) is not bool:
                return "artifact_invalid"
            if state.get("unavailable") or state.get("admission_policy") is None and has_markers(executable):
                return "legacy_policy_blocked"
            if size > 32 * 1024 * 1024:
                return "operational_capacity_blocked"
        return None
    if kind == "provider":
        try:
            source = ProviderSource.model_validate_json(json.dumps(payload, allow_nan=False))
            registry.provider_package(
                source.package, source.package_version, source.provider, source.schema_digest, source.adapter_version
            )
        except _ERRORS:
            return "provider_requirement_unsupported"
        return None
    if kind == "worker":
        try:
            release = WorkerRelease.model_validate_json(json.dumps(payload, allow_nan=False))
            registry.validate_release(release)
        except _ERRORS:
            return "worker_protocol_unsupported"
        return None
    return "inventory_incomplete"
