# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Trusted package declarations with pure metadata and native service identities.

Metadata owns canonical bytes; returned models cannot mutate the declaration.
Entry points export a declaration object, never a constructor or factory.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated, Literal

from pydantic import Field

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument, TaskCapability
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.connectors.descriptor import ConnectorDescriptor
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectorAdapter
from firefly_weave.contracts.definitions import (
    ConnectorDefinition,
    ContractModel,
    DistributionVersion,
    ResourceName,
    SemVer,
)
from firefly_weave.contracts.providers import provider_dispatch_kinds
from firefly_weave.contracts.values import JsonObject, JsonObjectData
from firefly_weave.contracts.workers import ConnectorBinding

ENTRY_POINT_GROUP = "firefly_weave.connectors"
RESERVED_ADAPTERS = frozenset(
    {
        "weave-http",
        "weave-postgresql",
        "weave-kafka",
        "weave-teams",
        "weave-whatsapp",
        "weave-telegram",
        "weave-slack",
        "weave-salesforce",
    }
)
type ServiceReference = Annotated[str, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")]


class PackageDocument(ContractModel):
    """Offline data contract; service references are never imported by validation."""

    format: Literal["weave/connector-package-v1"]
    distribution: Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")]
    version: SemVer
    distribution_version: DistributionVersion | None = None
    family: ResourceName
    provider: ResourceName | None = None
    protocol_versions: list[ResourceName]
    service: ServiceReference
    verifier_service: ServiceReference | None = None
    verification: Literal["implemented", "contract-tested", "local-integration-verified", "live-verified"] = (
        "implemented"
    )
    evidence: list[str] = Field(default_factory=list, max_length=100)
    manifest: ConnectorDefinition
    capabilities: list[TaskCapability]
    bindings: list[ConnectorBinding]
    event_schemas: dict[ResourceName, JsonObjectData] = Field(default_factory=dict, max_length=100)
    dispatch_event_kinds: list[ResourceName] | None = Field(default=None, min_length=1, max_length=100)


@dataclass(frozen=True)
class PackageMetadata:
    """Canonical package data validated with the compiler's exact schema policy."""

    canonical: FrozenDocument

    def __post_init__(self) -> None:
        doc = PackageDocument.model_validate(self.canonical.value)
        adapter = doc.manifest.spec.adapter
        self.validate_identity(doc)
        if not doc.protocol_versions or len(set(doc.protocol_versions)) != len(doc.protocol_versions):
            raise ValueError("Protocol versions must be explicit and unique")
        if doc.verification != "implemented" and not doc.evidence:
            raise ValueError("Verification status requires operation-specific evidence")
        if doc.event_schemas and not doc.verifier_service:
            raise ValueError("Event schemas require a verifier service")
        manifest = FrozenDocument.from_value(doc.manifest.model_dump(by_alias=True))
        catalog = CatalogSnapshot.from_definitions([], tasks=doc.capabilities, adapters=[adapter])
        result = compile_source(manifest.value, format="object", catalog=catalog)
        if not result.ok:
            raise ValueError("Invalid connector manifest/schema")
        refs = {f"{cap.task_type}@{cap.task_version}": cap for cap in doc.capabilities}
        if len(refs) != len(doc.capabilities) or len(doc.bindings) != len(doc.manifest.spec.actions):
            raise ValueError("Duplicate or missing capability binding")
        actions = set()
        bound_refs = set()
        for binding in doc.bindings:
            cap = refs.get(binding.task_reference)
            action = doc.manifest.spec.actions.get(binding.action)
            if (
                cap is None
                or action is None
                or binding.adapter != adapter
                or binding.connector_digest != manifest.digest
                or binding.implementation_version != doc.version
                or binding.action in actions
                or binding.task_reference in bound_refs
            ):
                raise ValueError("Manifest/capability binding drift")
            if (
                cap.input_schema != action.input_schema
                or cap.output_schema != action.output_schema
                or cap.side_effect != action.side_effect
                or cap.timeout_seconds != action.timeout_seconds
                or not cap.task_type.startswith("weave-connector-")
            ):
                raise ValueError("Manifest/capability contract drift")
            actions.add(binding.action)
            bound_refs.add(binding.task_reference)
        if bound_refs != set(refs):
            raise ValueError("Unbound capability")
        provider_dispatch_kinds(doc.event_schemas, doc.dispatch_event_kinds)
        for schema in doc.event_schemas.values():
            if validate_schema(schema, {}):
                raise ValueError("Invalid provider event schema")
        object.__setattr__(self, "canonical", FrozenDocument.from_value(doc.model_dump(by_alias=True)))

    def validate_identity(self, doc: PackageDocument) -> None:
        adapter = doc.manifest.spec.adapter
        if adapter in RESERVED_ADAPTERS or adapter.startswith("weave-"):
            raise ValueError("Reserved adapter identity")

    @property
    def document(self) -> JsonObject:
        return self.canonical.value

    @property
    def model(self) -> PackageDocument:
        return PackageDocument.model_validate(self.document)

    @property
    def descriptor(self) -> ConnectorDescriptor:
        doc = self.model
        return ConnectorDescriptor(
            FrozenDocument.from_value(doc.manifest.model_dump(by_alias=True)),
            doc.version,
            tuple(doc.capabilities),
            tuple(doc.bindings),
        )


@dataclass(frozen=True)
class ConnectorPackage:
    """Operator-trusted native service declarations; no service instance is constructed here."""

    metadata: PackageMetadata
    service_type: type
    verifier_service_type: type | None = None
    conformance: Callable[[ConnectorAdapter], Awaitable[None]] | None = None
    validate_connection: Callable[[ConnectionRequest], None] | None = None

    def __post_init__(self) -> None:
        from pyfly.container import Scope

        doc = self.metadata.model
        for service_type, reference in (
            (self.service_type, doc.service),
            (self.verifier_service_type, doc.verifier_service),
        ):
            if service_type is None and reference is None:
                continue
            if (
                not isinstance(service_type, type)
                or vars(service_type).get("__pyfly_stereotype__") != "service"
                or f"{service_type.__module__}:{service_type.__qualname__}" != reference
            ):
                raise ValueError("Package must declare the exact native @service type")
            scope = getattr(service_type, "__pyfly_scope__", None)
            # Native registration gives inherited refresh metadata precedence over the stereotype's scope.
            if scope is not Scope.SINGLETON or getattr(service_type, "__pyfly_refresh_scope__", False):
                raise ValueError("Connector services must have singleton scope")
        for method in ("execute", "test_connection"):
            if not callable(getattr(self.service_type, method, None)):
                raise ValueError("Invalid connector service contract")

    @property
    def descriptor(self) -> ConnectorDescriptor:
        return replace(self.metadata.descriptor, validate_connection=self.validate_connection)
