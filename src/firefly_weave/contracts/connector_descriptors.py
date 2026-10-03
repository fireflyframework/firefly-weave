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

"""Read-only wire views of the connector descriptors installed by the operator.

A view exposes only trusted deployment facts: the exact Connector manifest to publish, its
capabilities, bindings and schemas. It never carries configuration values or secret handles.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from pydantic import Field

from firefly_weave.compiler.catalog import TaskCapability
from firefly_weave.contracts.definitions import (
    ContractModel,
    PositiveInt,
    ResourceName,
    SemVer,
    SideEffect,
    VersionedReference,
)
from firefly_weave.contracts.values import JsonObjectData
from firefly_weave.contracts.workers import ConnectorBinding

if TYPE_CHECKING:
    from firefly_weave.connectors.descriptor import ConnectorDescriptor

# Every installable adapter identity is a ResourceName; the path form is bounded and cannot
# contain separators, encodings or dot segments.
ADAPTER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
ADAPTER = re.compile(ADAPTER_PATTERN)
type AdapterName = Annotated[str, Field(pattern=ADAPTER_PATTERN, max_length=128)]


class ConnectorActionView(ContractModel):
    name: ResourceName
    config_schema: JsonObjectData
    input_schema: JsonObjectData
    output_schema: JsonObjectData
    side_effect: SideEffect
    timeout_seconds: PositiveInt


class ConnectorConnectionView(ContractModel):
    config_schema: JsonObjectData
    auth_schema: JsonObjectData


class ConnectorDescriptorView(ContractModel):
    """One installed adapter; ``source`` is the exact publication source for ``definitions.publish``."""

    adapter: ResourceName
    reference: VersionedReference
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    manifest: JsonObjectData
    source: str
    implementation_version: SemVer
    capabilities: list[TaskCapability]
    bindings: list[ConnectorBinding]
    actions: list[ConnectorActionView]
    connection: ConnectorConnectionView
    published_version_id: UUID | None = None


def descriptor_view(descriptor: ConnectorDescriptor, published_version_id: UUID | None) -> ConnectorDescriptorView:
    """Build a view from a trusted ``ConnectorDescriptor``; values are owned copies."""
    manifest: dict[str, Any] = descriptor.manifest.value
    spec: dict[str, Any] = manifest["spec"]
    return ConnectorDescriptorView(
        adapter=spec["adapter"],
        reference=f"{manifest['metadata']['name']}@{manifest['metadata']['version']}",
        digest=descriptor.manifest.digest,
        manifest=manifest,
        source=descriptor.manifest.canonical.decode(),
        implementation_version=descriptor.implementation_version,
        capabilities=list(descriptor.capabilities),
        bindings=list(descriptor.bindings),
        actions=[
            ConnectorActionView(
                name=name,
                config_schema=action.get("configSchema", {"type": "object", "maxProperties": 0}),
                input_schema=action["inputSchema"],
                output_schema=action["outputSchema"],
                side_effect=action["sideEffect"],
                timeout_seconds=action["timeoutSeconds"],
            )
            for name, action in sorted(spec["actions"].items())
        ],
        connection=ConnectorConnectionView(config_schema=spec["configSchema"], auth_schema=spec["authSchema"]),
        published_version_id=published_version_id,
    )
