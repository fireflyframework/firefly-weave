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

"""Offline workflow composition using the canonical definition models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self, cast

from firefly_weave.contracts.definitions import (
    ConnectionRequirement,
    Expression,
    PositiveInt,
    ResourceName,
    SemVer,
    Step,
    WorkflowDefinition,
)
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonObject


def _snapshot(definition: WorkflowDefinition) -> WorkflowDefinition:
    # Frozen models can contain mutable lists/dicts or come from model_construct.
    return WorkflowDefinition.model_validate(definition.model_dump(by_alias=True, warnings="error"))


@dataclass(frozen=True, init=False)
class WorkflowBuilder:
    """Immutable fluent authoring; semantic validity still requires compilation.

    Construct steps, expressions and branches with contracts.definitions models.
    Each update returns a new builder. Required schemas and output are supplied
    at construction; an empty steps list follows the canonical workflow contract.
    """

    _definition: WorkflowDefinition = field(repr=False)

    def __init__(
        self,
        name: ResourceName,
        version: SemVer,
        *,
        input_schema: JsonObject,
        output_schema: JsonObject,
        output: Expression,
    ) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": name, "version": version},
                "spec": {"inputSchema": input_schema, "outputSchema": output_schema, "steps": [], "output": output},
            }
        )
        object.__setattr__(self, "_definition", _snapshot(definition))

    def _with_spec(self, **changes: object) -> Self:
        document = self._definition.model_dump(by_alias=True)
        document["spec"].update(changes)
        definition = _snapshot(WorkflowDefinition.model_validate(document))
        builder = object.__new__(type(self))
        object.__setattr__(builder, "_definition", definition)
        return builder

    def add_step(self, step: Step) -> Self:
        """Append any canonical step, including switch/parallel nested branches."""
        return self._with_spec(steps=[*self._definition.spec.steps, step])

    def with_output(self, output: Expression) -> Self:
        """Replace the workflow's canonical output expression."""
        return self._with_spec(output=output)

    def with_connection(self, name: ResourceName, requirement: ConnectionRequirement) -> Self:
        """Set a named canonical connection requirement."""
        return self._with_spec(connections={**self._definition.spec.connections, name: requirement})

    def with_llm_profile(self, name: ResourceName, profile: LLMProfile) -> Self:
        """Pin provider, model, reasoning and budgets in the workflow version."""
        return self._with_spec(llmProfiles={**(self._definition.spec.llm_profiles or {}), name: profile})

    def with_timeout(self, seconds: PositiveInt) -> Self:
        """Set a positive timeout; omission is the constructor's default."""
        return self._with_spec(timeoutSeconds=seconds)

    def to_definition(self) -> WorkflowDefinition:
        """Return a validated, detached canonical model."""
        return _snapshot(self._definition)

    def to_document(self) -> JsonObject:
        """Return a fresh JSON document with canonical aliases and omissions."""
        return cast(JsonObject, self.to_definition().model_dump(mode="json", by_alias=True))
