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

"""Builder ownership, canonical wire semantics and real compiler parity."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import (
    ActionStep,
    ArrayExpression,
    Branch,
    ConnectionRequirement,
    FailStep,
    LiteralExpression,
    ObjectExpression,
    Operation,
    OpExpression,
    ParallelStep,
    RefExpression,
    SignalStep,
    SwitchCase,
    SwitchStep,
    TransformStep,
    WaitStep,
    WorkflowDefinition,
)
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject

if TYPE_CHECKING:
    from firefly_weave.sdk.builder import WorkflowBuilder


def new_builder() -> WorkflowBuilder:
    builder_type = importlib.import_module("firefly_weave.sdk.builder").WorkflowBuilder
    return cast(
        "WorkflowBuilder",
        builder_type("example", "1.0.0", input_schema={}, output_schema={}, output=LiteralExpression(literal={})),
    )


def test_empty_canonical_roundtrip_and_omission() -> None:
    builder = new_builder()
    expected: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "example", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "connections": {}, "steps": [], "output": {"literal": {}}},
    }
    assert builder.to_document() == expected
    definition = builder.to_definition()
    assert isinstance(definition, WorkflowDefinition)
    assert definition == WorkflowDefinition.model_validate(expected)
    assert "timeoutSeconds" not in cast(JsonObject, builder.to_document()["spec"])


def test_all_step_kinds_and_nested_expressions_use_canonical_models() -> None:
    builder = (
        new_builder()
        .with_connection("primary", ConnectionRequirement(connector="echo@1.0.0", required=False))
        .with_timeout(120)
        .add_step(
            ActionStep.model_validate(
                {"id": "call", "kind": "action", "uses": "echo@1.0.0", "with": RefExpression(ref="/input")}
            )
        )
        .add_step(
            TransformStep(
                id="shape",
                kind="transform",
                value=ObjectExpression(object={"items": ArrayExpression(array=[LiteralExpression(literal=None)])}),
            )
        )
        .add_step(
            SwitchStep(
                id="choose",
                kind="switch",
                cases=[
                    SwitchCase(
                        when=OpExpression(op=Operation(name="exists", args=[RefExpression(ref="/input/flag")])),
                        steps=[WaitStep(id="pause", kind="wait", durationSeconds=3)],
                        output=LiteralExpression(literal=True),
                    )
                ],
                default=Branch(
                    steps=[FailStep(id="reject", kind="fail", code="REJECTED", message="Missing flag")],
                    output=LiteralExpression(literal=False),
                ),
            )
        )
        .add_step(
            ParallelStep(
                id="fork",
                kind="parallel",
                concurrency=2,
                branches={
                    "approval": Branch(
                        steps=[
                            SignalStep(
                                id="receive", kind="signal", name="approved", timeoutSeconds=30, payloadSchema={}
                            )
                        ],
                        output=RefExpression(ref="/steps/receive/output"),
                    )
                },
            )
        )
        .with_output(RefExpression(ref="/steps/fork/output"))
    )
    expected: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "example", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "connections": {"primary": {"connector": "echo@1.0.0", "required": False}},
            "timeoutSeconds": 120,
            "steps": [
                {"id": "call", "kind": "action", "uses": "echo@1.0.0", "with": {"ref": "/input"}},
                {"id": "shape", "kind": "transform", "value": {"object": {"items": {"array": [{"literal": None}]}}}},
                {
                    "id": "choose",
                    "kind": "switch",
                    "cases": [
                        {
                            "steps": [{"id": "pause", "kind": "wait", "durationSeconds": 3}],
                            "output": {"literal": True},
                            "when": {"op": {"name": "exists", "args": [{"ref": "/input/flag"}]}},
                        }
                    ],
                    "default": {
                        "steps": [{"id": "reject", "kind": "fail", "code": "REJECTED", "message": "Missing flag"}],
                        "output": {"literal": False},
                    },
                },
                {
                    "id": "fork",
                    "kind": "parallel",
                    "concurrency": 2,
                    "branches": {
                        "approval": {
                            "steps": [
                                {
                                    "id": "receive",
                                    "kind": "signal",
                                    "name": "approved",
                                    "timeoutSeconds": 30,
                                    "payloadSchema": {},
                                }
                            ],
                            "output": {"ref": "/steps/receive/output"},
                        }
                    },
                },
            ],
            "output": {"ref": "/steps/fork/output"},
        },
    }
    assert builder.to_document() == expected
    assert builder.to_definition() == WorkflowDefinition.model_validate(expected)


def test_input_models_exports_and_fluent_versions_are_detached() -> None:
    builder_type = importlib.import_module("firefly_weave.sdk.builder").WorkflowBuilder
    schema: JsonObject = {"type": "object", "properties": {"value": {"type": "string"}}}
    expression = LiteralExpression(literal={"value": [1]})
    initial = builder_type("owned", "1.0.0", input_schema=schema, output_schema={}, output=expression)
    nested = Branch(steps=[WaitStep(id="pause", kind="wait", durationSeconds=1)], output=expression)
    step = ParallelStep(id="fork", kind="parallel", branches={"one": nested}, concurrency=1)
    composed = initial.add_step(step)
    expected = composed.to_document()
    schema.clear()
    assert isinstance(expression.literal, dict)
    expression.literal.clear()
    nested.steps.clear()
    step.branches.clear()
    document = composed.to_document()
    document.clear()
    model = composed.to_definition()
    model.spec.steps.clear()
    model.spec.input_schema.clear()
    assert composed.to_document() == expected
    assert initial.to_definition().spec.steps == []
    changed = composed.with_timeout(9).with_output(LiteralExpression(literal=False))
    assert changed.to_definition().spec.timeout_seconds == 9
    assert changed.to_definition().spec.output == LiteralExpression(literal=False)
    assert composed.to_document() == expected


@pytest.mark.parametrize("invalid", [None, True, 0, -1, "5", 1.5, MAX_SAFE_INTEGER + 1])
def test_timeout_does_not_coerce_or_accept_explicit_null(invalid: object) -> None:
    builder = new_builder()
    original = builder.to_document()
    with pytest.raises(ValidationError):
        builder.with_timeout(invalid)  # type: ignore[arg-type]
    assert builder.to_document() == original


@pytest.mark.parametrize("field,value", [("name", "invalid name"), ("version", "latest"), ("input_schema", None)])
def test_constructor_enforces_required_canonical_fields(field: str, value: object) -> None:
    builder_type = importlib.import_module("firefly_weave.sdk.builder").WorkflowBuilder
    arguments = {
        "name": "example",
        "version": "1.0.0",
        "input_schema": {},
        "output_schema": {},
        "output": LiteralExpression(literal={}),
    }
    arguments[field] = value
    with pytest.raises(ValidationError):
        builder_type(**arguments)


def test_forged_or_mutated_models_and_callbacks_cannot_bypass_validation() -> None:
    builder = new_builder()
    invalid_step = WaitStep.model_construct(id="pause", kind="wait", duration_seconds=0)
    with pytest.raises(ValidationError):
        builder.add_step(invalid_step)
    invalid_expression = LiteralExpression(literal=[])
    assert isinstance(invalid_expression.literal, list)
    invalid_expression.literal.append(MAX_SAFE_INTEGER + 1)
    with pytest.raises(ValidationError):
        builder.with_output(invalid_expression)
    invalid_connection = ConnectionRequirement.model_construct(connector="unversioned")
    with pytest.raises(ValidationError):
        builder.with_connection("primary", invalid_connection)
    with pytest.raises(ValidationError):
        builder.with_connection("invalid name", ConnectionRequirement(connector="echo@1.0.0"))
    invoked = False

    def callback() -> LiteralExpression:
        nonlocal invoked
        invoked = True
        return LiteralExpression(literal={})

    with pytest.raises(ValidationError):
        builder.with_output(callback)  # type: ignore[arg-type]
    assert not invoked


def onboarding_builder(document: JsonObject) -> WorkflowBuilder:
    builder_type = importlib.import_module("firefly_weave.sdk.builder").WorkflowBuilder
    spec = cast(JsonObject, document["spec"])
    builder = builder_type(
        "customer-onboarding",
        "1.0.0",
        input_schema=spec["inputSchema"],
        output_schema=spec["outputSchema"],
        output=RefExpression(ref="/steps/decision/output"),
    )
    return cast(
        "WorkflowBuilder",
        builder.with_timeout(172800)
        .add_step(
            ActionStep.model_validate(
                {
                    "id": "check",
                    "kind": "action",
                    "uses": "onboarding.check-customer@1.0.0",
                    "with": ObjectExpression(object={"customerId": RefExpression(ref="/input/customerId")}),
                }
            )
        )
        .add_step(
            SignalStep(
                id="approval",
                kind="signal",
                name="customer-approved",
                timeoutSeconds=86400,
                payloadSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["approved"],
                    "properties": {"approved": {"type": "boolean"}},
                },
            )
        )
        .add_step(
            TransformStep(
                id="decision",
                kind="transform",
                value=ObjectExpression(
                    object={
                        "accepted": OpExpression(
                            op=Operation(
                                name="and",
                                args=[
                                    RefExpression(ref="/steps/check/output/eligible"),
                                    RefExpression(ref="/steps/approval/output/approved"),
                                ],
                            )
                        )
                    }
                ),
            )
        ),
    )


def test_real_fixture_compilation_digest_diagnostics_and_locations_match(
    fixture_dir: Path, catalog: CatalogSnapshot
) -> None:
    manual = cast(JsonObject, yaml.safe_load((fixture_dir / "definitions/valid/onboarding.workflow.yaml").read_text()))
    builder = onboarding_builder(manual)
    hand_authored = compile_source(manual, format="object", catalog=catalog, filename="onboarding")
    built = compile_source(builder.to_document(), format="object", catalog=catalog, filename="onboarding")
    assert built.ok and hand_authored.ok
    assert built.diagnostics == hand_authored.diagnostics
    assert built.artifact is not None and hand_authored.artifact is not None
    assert built.artifact.digest == hand_authored.artifact.digest
    assert built.artifact.source_map == hand_authored.artifact.source_map == {}


@pytest.mark.parametrize("scenario", ["missing-catalog-action", "duplicate-step", "unknown-reference", "limit"])
def test_compiler_rejections_and_limits_match_manual_document(scenario: str) -> None:
    builder = new_builder().add_step(WaitStep(id="pause", kind="wait", durationSeconds=1))
    manual: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "example", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [{"id": "pause", "kind": "wait", "durationSeconds": 1}],
            "output": {"literal": {}},
        },
    }
    spec = cast(JsonObject, manual["spec"])
    steps = cast(list[JsonObject], spec["steps"])
    if scenario == "missing-catalog-action":
        builder = builder.add_step(
            ActionStep.model_validate(
                {"id": "call", "kind": "action", "uses": "absent@1.0.0", "with": LiteralExpression(literal={})}
            )
        )
        steps.append({"id": "call", "kind": "action", "uses": "absent@1.0.0", "with": {"literal": {}}})
    elif scenario == "duplicate-step":
        builder = builder.add_step(WaitStep(id="pause", kind="wait", durationSeconds=1))
        steps.append({"id": "pause", "kind": "wait", "durationSeconds": 1})
    elif scenario == "unknown-reference":
        builder = builder.with_output(RefExpression(ref="/steps/missing/output"))
        spec["output"] = {"ref": "/steps/missing/output"}
    limits = Limits(max_document_nodes=1) if scenario == "limit" else Limits()
    built = compile_source(builder.to_document(), format="object", catalog=CatalogSnapshot.empty(), limits=limits)
    expected = compile_source(manual, format="object", catalog=CatalogSnapshot.empty(), limits=limits)
    assert not built.ok and not expected.ok
    assert built.artifact is expected.artifact is None
    assert built.diagnostics == expected.diagnostics
    assert built.error_count == expected.error_count > 0


def test_offline_import_and_compilation_with_optional_dependencies_blocked() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
blocked = {'pyfly', 'httpx', 'httpcore', 'sqlalchemy', 'asyncpg', 'alembic', 'jwt', 'croniter', 'opentelemetry'}
class NoOptionalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked or fullname.startswith(('firefly_weave.api', 'firefly_weave.persistence')):
            raise ImportError('Optional dependency attempted: ' + fullname)
sys.meta_path.insert(0, NoOptionalImports())
from firefly_weave.sdk.builder import WorkflowBuilder
from firefly_weave.contracts.definitions import LiteralExpression
builder = WorkflowBuilder('offline', '1.0.0', input_schema={}, output_schema={}, output=LiteralExpression(literal={}))
assert not any(name.split('.')[0] in blocked for name in sys.modules)
assert 'firefly_weave.compiler.api' not in sys.modules
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
assert compile_source(builder.to_document(), format='object', catalog=CatalogSnapshot.empty()).ok
assert not any(name.split('.')[0] in blocked for name in sys.modules)
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_document_is_plain_interoperable_json() -> None:
    builder = new_builder().with_output(LiteralExpression(literal={"unicode": "café", "empty": None}))
    assert json.loads(json.dumps(builder.to_document(), allow_nan=False)) == builder.to_document()
