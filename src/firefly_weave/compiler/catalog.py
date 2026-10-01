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

"""Explicit, content-addressed compiler inputs. Never discovers live services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Literal, cast

import rfc8785
from pydantic import Field, TypeAdapter, model_validator

from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.definitions import (
    ActionDefinition,
    ConnectorDefinition,
    ContractModel,
    Definition,
    PositiveInt,
    ResourceName,
    SemVer,
    SideEffect,
    WorkflowDefinition,
    load_definition,
)
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonObjectData, UnicodeString

DefinitionModel = WorkflowDefinition | ActionDefinition | ConnectorDefinition
ResourceKind = Literal["Workflow", "Action", "Connector", "TaskCapability", "Adapter", "Schema"]
_NAME_ADAPTER: TypeAdapter[str] = TypeAdapter(ResourceName)
_STORAGE_LIMITS = Limits(max_depth=128)


@dataclass(frozen=True)
class FrozenDocument:
    """Canonical immutable storage; every value access returns an owned JSON tree."""

    canonical: bytes

    def __post_init__(self) -> None:
        if type(self.canonical) is not bytes or len(self.canonical) > _STORAGE_LIMITS.max_payload_bytes:
            raise ValueError("Frozen document requires bounded immutable bytes")
        try:
            value = json.loads(self.canonical)
            if type(value) is not dict:
                raise ValueError("Frozen document must be a JSON object")
            measure_value(value, limits=_STORAGE_LIMITS)
            if rfc8785.dumps(value) != self.canonical:
                raise ValueError("Frozen document must be canonical JSON")
        except (RecursionError, UnicodeError) as error:
            raise ValueError("Invalid frozen document") from error

    @classmethod
    def from_value(cls, value: JsonObject) -> FrozenDocument:
        measure_value(value, limits=_STORAGE_LIMITS)
        return cls(rfc8785.dumps(value))

    @property
    def value(self) -> JsonObject:
        return cast(JsonObject, json.loads(self.canonical))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


class TaskCapability(ContractModel):
    """Published task contract, independent of worker instance/release readiness."""

    task_type: ResourceName = Field(alias="taskType")
    task_version: SemVer = Field(alias="taskVersion")
    input_schema: JsonObjectData = Field(alias="inputSchema")
    output_schema: JsonObjectData = Field(alias="outputSchema")
    side_effect: SideEffect = Field(alias="sideEffect")
    timeout_seconds: PositiveInt = Field(alias="timeoutSeconds")


@dataclass(frozen=True)
class CatalogResource:
    kind: ResourceKind
    reference: str
    definition: FrozenDocument

    def __post_init__(self) -> None:
        value = self.definition.value
        if self.kind in {"Workflow", "Action", "Connector"}:
            model = load_definition(value)
            expected = f"{model.metadata.name}@{model.metadata.version}"
            if model.kind != self.kind or self.reference != expected:
                raise ValueError("Catalog resource identity mismatch")
            object.__setattr__(self, "definition", FrozenDocument.from_value(model.model_dump(by_alias=True)))
        elif self.kind == "TaskCapability":
            task = TaskCapability.model_validate(value)
            if self.reference != f"{task.task_type}@{task.task_version}":
                raise ValueError("Catalog task identity mismatch")
        elif self.kind == "Adapter":
            _NAME_ADAPTER.validate_python(self.reference)
            if value != {"adapter": self.reference}:
                raise ValueError("Catalog adapter identity mismatch")
        elif self.kind != "Schema":
            raise ValueError("Unknown catalog resource kind")

    @property
    def digest(self) -> str:
        return self.definition.digest


@dataclass(frozen=True)
class CatalogSnapshot:
    resources: Mapping[tuple[ResourceKind, str], CatalogResource]
    schemas: Mapping[str, FrozenDocument]

    def __post_init__(self) -> None:
        # Even direct construction cannot retain the caller's mutable mappings.
        for key, resource in self.resources.items():
            if key != (resource.kind, resource.reference):
                raise ValueError("Catalog identity mismatch")
        resources = dict(self.resources)
        schemas = dict(self.schemas)
        for (kind, name), resource in resources.items():
            if kind == "Schema":
                if name in schemas and schemas[name].digest != resource.digest:
                    raise ValueError("Catalog schema identity mismatch")
                schemas[name] = resource.definition
        for name, schema in schemas.items():
            resources[("Schema", name)] = CatalogResource("Schema", name, schema)
        object.__setattr__(self, "resources", MappingProxyType(resources))
        object.__setattr__(self, "schemas", MappingProxyType(schemas))

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls({}, {})

    @classmethod
    def from_definitions(
        cls,
        definitions: Iterable[DefinitionModel],
        *,
        tasks: Iterable[JsonObject | TaskCapability] = (),
        adapters: Iterable[str] = (),
        schema_bundle: Mapping[str, JsonObject] | None = None,
    ) -> CatalogSnapshot:
        resources: dict[tuple[ResourceKind, str], CatalogResource] = {}

        def add(kind: ResourceKind, reference: str, value: JsonObject) -> None:
            resource = CatalogResource(kind, reference, FrozenDocument.from_value(value))
            key = kind, reference
            if key in resources and resources[key].digest != resource.digest:
                raise ValueError("Immutable catalog version conflict")
            resources[key] = resource

        for definition in definitions:
            value = definition.model_dump(by_alias=True)
            model = load_definition(value)
            add(model.kind, f"{model.metadata.name}@{model.metadata.version}", value)
        for task in tasks:
            capability = TaskCapability.model_validate(
                task.model_dump(by_alias=True) if isinstance(task, TaskCapability) else task
            )
            add(
                "TaskCapability",
                f"{capability.task_type}@{capability.task_version}",
                capability.model_dump(by_alias=True),
            )
        for adapter in adapters:
            add("Adapter", adapter, {"adapter": adapter})
        schemas = {name: FrozenDocument.from_value(value) for name, value in (schema_bundle or {}).items()}
        for name, schema in schemas.items():
            add("Schema", name, schema.value)
        return cls(resources, schemas)

    @classmethod
    def from_lock(cls, lock: JsonObject) -> CatalogSnapshot:
        """Read an already parsed lock; supplied definition digests are mandatory."""
        if set(lock) != {"definitions", "tasks", "adapters", "schemas"}:
            raise ValueError("Invalid catalog lock fields")
        if type(lock["definitions"]) is not list or type(lock["tasks"]) is not list:
            raise ValueError("Invalid catalog lock collections")
        if type(lock["adapters"]) is not list or type(lock["schemas"]) is not dict:
            raise ValueError("Invalid catalog lock collections")
        definitions = []
        for entry in lock["definitions"]:
            if type(entry) is not dict or set(entry) != {"document", "digest"} or type(entry["document"]) is not dict:
                raise ValueError("Invalid catalog definition")
            model = load_definition(entry["document"])
            if FrozenDocument.from_value(model.model_dump(by_alias=True)).digest != entry["digest"]:
                raise ValueError("Catalog definition digest mismatch")
            definitions.append(model)
        return cls.from_definitions(
            definitions,
            tasks=cast(list[JsonObject], lock["tasks"]),
            adapters=cast(list[str], lock["adapters"]),
            schema_bundle=cast(dict[str, JsonObject], lock["schemas"]),
        )

    def resolve(self, kind: ResourceKind, reference: str) -> CatalogResource | None:
        return self.resources.get((kind, reference))


class CatalogLockDefinition(ContractModel):
    document: Definition
    digest: Annotated[UnicodeString, Field(pattern=r"^[a-f0-9]{64}$")]


class CatalogLock(ContractModel):
    """Published wire shape, with existing catalog identity and digest checks."""

    definitions: list[CatalogLockDefinition]
    tasks: list[TaskCapability]
    adapters: list[ResourceName]
    schemas: dict[UnicodeString, JsonObjectData]

    @model_validator(mode="after")
    def valid_snapshot(self) -> CatalogLock:
        CatalogSnapshot.from_lock(self.model_dump(by_alias=True))
        return self
