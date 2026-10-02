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

"""Bounded lowering from immutable analysis to explicit control-flow IR."""

from __future__ import annotations

from typing import cast

from pydantic import Field, TypeAdapter

from firefly_weave.compiler.analyzer import AnalysisResult, AnalyzedStep
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import HUMAN_IR_VERSION, IR_VERSION, Executable
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonValue


class ArtifactLimits(ContractModel):
    """Generated storage policy, independent of source/schema/runtime payload limits."""

    max_bytes: int = Field(default=33_554_432, gt=0)
    max_nodes: int = Field(default=2_000_000, gt=0)
    max_depth: int = Field(default=128, gt=0)

    def value_limits(self) -> Limits:
        return Limits(
            max_source_bytes=self.max_bytes,
            max_payload_bytes=self.max_bytes,
            max_document_nodes=self.max_nodes,
            max_depth=self.max_depth,
        )


DEFAULT_ARTIFACT_LIMITS = ArtifactLimits()
_EXECUTABLE: TypeAdapter[Executable] = TypeAdapter(Executable)


class ConstructionBudget:
    """Charge distinct fragments before inserting them into the generated tree.

    Bytes and nodes accumulate across fragments; final whole-tree measurement
    includes wrapper overhead and applies the exact depth ceiling before sealing.
    """

    def __init__(self, limits: ArtifactLimits) -> None:
        self.limits = limits
        self.bytes = 0
        self.nodes = 0

    def charge(self, value: JsonObject) -> None:
        self.bytes += measure_value(value, limits=self.limits.value_limits())
        if self.bytes > self.limits.max_bytes:
            raise ValueError("Generated artifact storage budget exceeded")
        pending: list[JsonValue] = [value]
        while pending:
            item = pending.pop()
            self.nodes += 1
            if self.nodes > self.limits.max_nodes:
                raise ValueError("Generated artifact node budget exceeded")
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)


class _Lowerer:
    def __init__(self, result: AnalysisResult, limits: ArtifactLimits) -> None:
        self.result = result
        self.budget = ConstructionBudget(limits)
        self.schemas: JsonObject = {}
        self.nodes: list[JsonValue] = []
        self.edges: list[JsonValue] = []
        self.actions = {r.reference: r.digest for r in result.resolved_resources if r.kind == "Action"}

    def schema(self, document: FrozenDocument) -> str:
        digest = document.digest
        if digest not in self.schemas:
            # The immutable document has already bounded its own materialization.
            self.budget.charge({"schema": digest, "bytes": len(document.canonical)})
            if self.budget.bytes + len(document.canonical) > self.budget.limits.max_bytes:
                raise ValueError("Generated schema table budget exceeded")
            value = document.value
            self.budget.charge(value)
            self.schemas[digest] = value
        return digest

    def node(self, value: JsonObject) -> None:
        self.budget.charge(value)
        self.nodes.append(value)

    def edge(self, source: str, target: str, kind: str = "next", branch: str | None = None) -> None:
        edge: JsonObject = {"source": source, "target": target, "kind": kind, "branch": branch}
        self.budget.charge(edge)
        self.edges.append(edge)

    def block(self, steps: tuple[AnalyzedStep, ...], successor: str) -> str:
        entry = steps[0].id if steps else successor
        for i, step in enumerate(steps):
            following = steps[i + 1].id if i + 1 < len(steps) else successor
            value = step.definition.value
            base: JsonObject = {"id": step.id, "kind": step.kind, "scope": list(step.scope), "path": step.path}
            if step.kind in {"switch", "parallel"}:
                join_id = "@join:" + step.id
                branch_ids: list[JsonValue] = []
                branches: list[JsonValue] = []
                cases = cast(list[JsonObject], value.get("cases", []))
                default = ""
                for index, branch in enumerate(step.branches):
                    output_id = f"@branch:{step.id}:{index}"
                    branch_ids.append(output_id)
                    target = self.block(branch.steps, output_id)
                    self.node(
                        {
                            "id": output_id,
                            "kind": "branch-output",
                            "scope": cast(list[JsonValue], list(step.scope) + [step.id, branch.name]),
                            "path": branch.path + "/output",
                            "owner": step.id,
                            "branch": branch.name,
                            "output": branch.output.value,
                            "completes": branch.completes,
                        }
                    )
                    if branch.completes:
                        self.edge(output_id, join_id, "join", branch.name)
                    if step.kind == "parallel":
                        branches.append({"name": branch.name, "target": target})
                        self.edge(step.id, target, "fork", branch.name)
                    elif branch.name == "default":
                        default = target
                        self.edge(step.id, target, "default", "default")
                    else:
                        branches.append({"branch": branch.name, "when": cases[index]["when"], "target": target})
                        self.edge(step.id, target, "case", branch.name)
                self.node(
                    {
                        "id": join_id,
                        "kind": "join",
                        "scope": list(step.scope),
                        "path": step.path,
                        "owner": step.id,
                        "mode": "all" if step.kind == "parallel" else "selected",
                        "branches": branch_ids,
                        "completes": step.completes,
                    }
                )
                base["join"] = join_id
                if step.kind == "parallel":
                    base.update({"branches": branches, "concurrency": value["concurrency"]})
                else:
                    base.update({"cases": branches, "default": default})
                if step.completes:
                    self.edge(join_id, following)
            else:
                if step.kind == "action":
                    base.update(
                        {
                            "dependency": self.actions[cast(str, value["uses"])],
                            "with": value["with"],
                            "connection": value.get("connection"),
                        }
                    )
                elif step.kind == "signal":
                    base.update(
                        {
                            "name": value["name"],
                            "timeoutSeconds": value["timeoutSeconds"],
                            "schemaRef": self.schema(step.output_schema),
                        }
                    )
                else:
                    base.update({k: v for k, v in value.items() if k not in {"id", "kind"}})
                if step.completes:
                    self.edge(step.id, following)
            self.node(base)
        return entry

    def run(self) -> JsonObject:
        if not self.result.ok or self.result.definition is None:
            raise ValueError("Lowering requires complete successful analysis")
        definition = self.result.definition.value
        dependencies: list[JsonValue] = []
        for resource in self.result.resolved_resources:
            if self.budget.bytes + len(resource.definition.canonical) > self.budget.limits.max_bytes:
                raise ValueError("Generated dependency table budget exceeded")
            dependency: JsonObject = {
                "kind": resource.kind,
                "reference": resource.reference,
                "digest": resource.digest,
                "document": resource.definition.value,
            }
            self.budget.charge(dependency)
            dependencies.append(dependency)
        guards: list[JsonValue] = []
        for guard in self.result.runtime_guards:
            item: JsonObject = {"path": guard.path, "purpose": guard.purpose, "schemaRef": self.schema(guard.schema)}
            self.budget.charge(item)
            guards.append(item)
        value: JsonObject = {
            "irVersion": IR_VERSION,
            "apiVersion": definition["apiVersion"],
            "kind": definition["kind"],
            "metadata": definition["metadata"],
            "dependencies": dependencies,
            "schemas": self.schemas,
            "guards": guards,
        }
        spec = cast(JsonObject, definition["spec"])
        if self.result.kind == "Workflow":
            self.node({"id": "@start", "kind": "start", "scope": [], "path": "/spec"})
            self.node({"id": "@end", "kind": "end", "scope": [], "path": "/spec/output", "output": spec["output"]})
            first = self.block(self.result.typed_graph, "@end")
            self.edge("@start", first)
            value.update(
                {
                    "inputSchema": self.schema(FrozenDocument.from_value(cast(JsonObject, spec["inputSchema"]))),
                    "outputSchema": self.schema(FrozenDocument.from_value(cast(JsonObject, spec["outputSchema"]))),
                    "connections": spec["connections"],
                    "timeoutSeconds": spec.get("timeoutSeconds"),
                    "graph": cast(
                        JsonObject, {"entry": "@start", "exit": "@end", "nodes": self.nodes, "edges": self.edges}
                    ),
                }
            )
        else:
            self.budget.charge(spec)
            value["spec"] = spec
        if any(isinstance(node, dict) and node.get("kind") == "humanTask" for node in self.nodes):
            value["irVersion"] = HUMAN_IR_VERSION
        measure_value(value, limits=self.budget.limits.value_limits())
        return cast(JsonObject, _EXECUTABLE.validate_python(value).model_dump(by_alias=True))


def lower(result: AnalysisResult, *, limits: ArtifactLimits = DEFAULT_ARTIFACT_LIMITS) -> JsonObject:
    return _Lowerer(result, limits).run()
