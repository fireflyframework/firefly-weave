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

"""Execute step, Execute previous steps and Execute workflow on the Studio host: the slice of a workflow a target
needs, the catalog a simulation compiles against, stubs for what isn't there, and the simulated run as a trace.

Pure: no network, no clock and no files. The host fetches the project catalog itself and passes it in.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.values import JsonObject, JsonValue

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact
    from firefly_weave.compiler.ir import WorkflowIR
    from firefly_weave.contracts.transactions import UndoPhase
    from firefly_weave.operations.debug.models import DebugState, TransactionDebugView
    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.operations.debug.transactions import PhaseTarget
    from firefly_weave.runtime.kernel import PreparedWorkflow
    from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent, TransactionRecord
    from firefly_weave.runtime.transactions import TransactionShapes
    from firefly_weave.studio.local_contracts import (
        CompileSummary,
        ExecuteRequest,
        ExecuteResponse,
        SimulationRequest,
        TraceEntry,
    )

NULL: JsonObject = {"literal": None}

logger = logging.getLogger(__name__)


def _branches(step: JsonObject) -> list[JsonObject]:
    """The step lists a container holds, each with its own ``steps`` and ``output``."""
    kind = step.get("kind")
    # A step's error path is a block too; it comes after the step's own blocks.
    path = [cast(JsonObject, step["errorPath"])] if isinstance(step.get("errorPath"), dict) else []
    if kind == "switch":
        return [*cast(list[JsonObject], step.get("cases") or []), cast(JsonObject, step.get("default") or {})]
    if kind == "parallel":
        return list(cast(dict[str, JsonObject], step.get("branches") or {}).values())
    if kind == "forEach":
        return [cast(JsonObject, step.get("body") or {})]
    if kind == "transaction":
        return [step, *path]
    return path


def steps_of(steps: object) -> Iterator[JsonObject]:
    """Every step of a step list, nested ones included, in document order."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if isinstance(step, dict):
            yield step
            for branch in _branches(step):
                yield from steps_of(branch.get("steps"))


def _cut(steps: list[JsonObject], step_id: str) -> list[JsonObject] | None:
    """The steps up to ``step_id``: those before it, then it (or the container holding it, cut inside)."""
    for index, step in enumerate(steps):
        if step.get("id") == step_id:
            return steps[: index + 1]
        inner = _cut_inside(step, step_id)
        if inner is not None:
            return [*steps[:index], inner]
    return None


def _cut_inside(step: JsonObject, step_id: str) -> JsonObject | None:
    for position, branch in enumerate(_branches(step)):
        steps = branch.get("steps")
        cut = _cut(cast(list[JsonObject], steps), step_id) if isinstance(steps, list) else None
        if cut is None:
            continue
        kept = copy.deepcopy(step)
        holders = _branches(kept)
        for other, sibling in enumerate(holders):
            if other != position and holders[position] is kept.get("errorPath") and sibling is kept:
                # The target is in a transaction's error path, which runs only after its body: the body stays.
                continue
            # The branch holding the target ends after it; its siblings run nothing. Conditions stay, so the
            # test data still decides which path a decision takes.
            sibling["steps"] = cast(JsonValue, cut if other == position else [])
            sibling["output"] = copy.deepcopy(NULL)
        return kept
    return None


def slice_for_target(document: JsonObject, step_id: str) -> JsonObject | None:
    """The workflow a target step needs, or None when the step isn't in it.

    Kept: every step before the target in its sequence and in each enclosing sequence, and the enclosing groups with
    their other branches emptied and the target's branch cut after it. The workflow's output becomes null, so a step
    later in the workflow, misconfigured or not, never blocks testing an earlier one.
    """
    spec = document.get("spec")
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not isinstance(steps, list):
        return None
    cut = _cut(cast(list[JsonObject], steps), step_id)
    if cut is None:
        return None
    sliced = copy.deepcopy(document)
    sliced_spec = cast(JsonObject, sliced["spec"])
    sliced_spec["steps"] = cast(JsonValue, copy.deepcopy(cut))
    sliced_spec["output"] = copy.deepcopy(NULL)
    sliced_spec["outputSchema"] = {}
    return sliced


# --- the catalog a simulation compiles against ------------------------------------------------------------------

STUB_TASK = "studio.stub"
STUB_VERSION = "0.0.0"


class CatalogConflict(ValueError):
    """The same name and version with another digest (409 ``WV-STUDIO-CATALOG-CONFLICT``)."""

    def __init__(self, reference: str) -> None:
        self.reference = reference
        super().__init__(reference)


@functools.cache
def bundled_catalog() -> CatalogSnapshot:
    """What ships with Weave: the HTTP, email and file connectors, the AI action and the AI agent's turn action, so
    they compile offline."""
    from firefly_weave.connectors import email
    from firefly_weave.contracts import agentic
    from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
    from firefly_weave.contracts.definitions import ConnectorDefinition
    from firefly_weave.contracts.file_connectors import FILE_DESCRIPTORS
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
    from firefly_weave.sdk.http_actions import HTTP_ADAPTER

    descriptors = [HTTP_PROFILE_DESCRIPTOR, email.package.descriptor, *FILE_DESCRIPTORS.values()]
    adapters = [
        HTTP_ADAPTER,
        *(cast(str, cast(JsonObject, d.manifest.value["spec"])["adapter"]) for d in descriptors[1:]),
    ]
    return CatalogSnapshot.from_definitions(
        [
            *(ConnectorDefinition.model_validate(d.manifest.value) for d in descriptors),
            load_definition(agentic.action_definition()),
            load_definition(turn_action_definition()),
            load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value),
        ],
        tasks=[*(task for d in descriptors for task in d.capabilities), agentic.task_capability(), turn_capability()],
        adapters=[*adapters, "weave-agentic-provider"],
    )


def _union(snapshots: Iterable[CatalogSnapshot]) -> CatalogSnapshot:
    resources: dict[tuple[Any, str], CatalogResource] = {}
    for snapshot in snapshots:
        for key, resource in snapshot.resources.items():
            if key in resources and resources[key].digest != resource.digest:
                raise CatalogConflict(key[1])
            resources[key] = resource
    return CatalogSnapshot(resources, {})


def _task_of(action: ActionDefinition) -> JsonObject | None:
    """The task contract a local worker action implies; the platform checks the real worker when it is published."""
    value = cast(JsonObject, action.model_dump(by_alias=True, mode="json"))
    spec = cast(JsonObject, value["spec"])
    implementation = cast(JsonObject, spec["implementation"])
    if implementation.get("kind") != "worker":
        return None
    return {
        "taskType": implementation["taskType"],
        "taskVersion": implementation["taskVersion"],
        "inputSchema": spec["inputSchema"],
        "outputSchema": spec["outputSchema"],
        "sideEffect": spec["sideEffect"],
        "timeoutSeconds": spec["timeoutSeconds"],
    }


def _stub_action(reference: str) -> JsonObject:
    name, _, version = reference.partition("@")
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": name, "version": version},
        "spec": {
            "implementation": {"kind": "worker", "taskType": STUB_TASK, "taskVersion": STUB_VERSION},
            "sideEffect": "read_only",
            "timeoutSeconds": 60,
            "inputSchema": {},
            "outputSchema": {},
        },
    }


STUB_CAPABILITY: JsonObject = {
    "taskType": STUB_TASK,
    "taskVersion": STUB_VERSION,
    "inputSchema": {},
    "outputSchema": {},
    "sideEffect": "read_only",
    "timeoutSeconds": 60,
}


def _stub_workflow(reference: str) -> JsonObject:
    """A called workflow nobody has, as an interface any workflow may call; its output comes from a pin."""
    name, _, version = reference.partition("@")
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": version},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [], "output": {"literal": None}, "callable": {}},
    }


def simulation_catalog(
    document: JsonObject,
    definitions: Iterable[JsonObject],
    project: CatalogSnapshot | None,
    *,
    stub_unresolved: bool,
) -> tuple[CatalogSnapshot, list[str]]:
    """The union of the project catalog (when connected), what ships with Weave and the local definitions Studio
    sent, plus, with ``stub_unresolved``, a read-only stub for each action the workflow uses that none of them has and
    an interface for each called workflow none of them has. Decision tables are never stubbed.

    Returns the catalog and the stubbed references, sorted. A local definition that doesn't load is left out, so the
    steps that use it are stubbed or fail to compile, never accepted on a guess.
    """
    local = []
    tasks: list[JsonObject] = []
    for value in definitions:
        try:
            model = load_definition(value)
        except ValueError:
            continue
        local.append(model)
        if isinstance(model, ActionDefinition) and (task := _task_of(model)) is not None:
            tasks.append(task)
    try:
        own = CatalogSnapshot.from_definitions(local)
    except ValueError as error:
        raise CatalogConflict(str(error)) from None
    parts = [bundled_catalog(), *([project] if project is not None else []), own]
    merged = _union(parts)
    missing_tasks = [
        t for t in tasks if ("TaskCapability", f"{t['taskType']}@{t['taskVersion']}") not in merged.resources
    ]
    if missing_tasks:
        merged = _union([merged, CatalogSnapshot.from_definitions([], tasks=missing_tasks)])
    if not stub_unresolved:
        return merged, []
    actions: set[str] = set()
    callees: set[str] = set()
    spec = document.get("spec")
    for step in steps_of(spec.get("steps") if isinstance(spec, dict) else None):
        uses = step.get("uses")
        if not isinstance(uses, str):
            continue
        if step.get("kind") == "action" and ("Action", uses) not in merged.resources:
            actions.add(uses)
        elif step.get("kind") == "callWorkflow" and ("Workflow", uses) not in merged.resources:
            callees.add(uses)
    if actions or callees:
        merged = _union(
            [
                merged,
                CatalogSnapshot.from_definitions(
                    [
                        *(load_definition(_stub_action(reference)) for reference in sorted(actions)),
                        *(load_definition(_stub_workflow(reference)) for reference in sorted(callees)),
                    ],
                    tasks=[STUB_CAPABILITY] if actions else [],
                ),
            ]
        )
    return merged, sorted(actions | callees)


# --- the simulated run -------------------------------------------------------------------------------------------


class SimulationRefused(ValueError):
    """A request the host answers with a problem: ``status``, ``code`` and, for values, JSON pointers only."""

    def __init__(self, status: int, code: str, message: str, paths: Iterable[str] = ()) -> None:
        self.status, self.code, self.message, self.paths = status, code, message, list(paths)
        super().__init__(code)


ROUNDS = 1000


def _pointer(*parts: str) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in parts)


def classify_test_data(
    document: JsonObject,
    request: SimulationRequest,
    catalog: CatalogSnapshot,
    callees: Mapping[str, JsonObject] | None = None,
) -> tuple[list[str], list[str]]:
    """Pointers into the request of values a schema marks secret, and of values too large or too deep to classify.

    Only typed values are classified, each against its own schema: the input, each step mock by its step's output
    fields (a confirm's, cancel's or undo's by that operation's own), each action mock by its action's, each AI agent
    tool's mock by every schema of the tool's result and a mock of one agent call for secrets by every tool's, each
    mock inside a called workflow by that workflow's step, each signal payload, each person's answer, each saved AI
    turn's final answer by its profile's output fields, and each saved tool call's arguments by its tool's input. Both
    lists are refused before anything is simulated.
    """
    from firefly_weave.compiler.agent_tools import offline_input_schema
    from firefly_weave.compiler.platform_catalog import platform_schema_bundle
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.compiler.test_data import pin_target, step_at
    from firefly_weave.operations.debug.ai_turns import tool_values
    from firefly_weave.operations.redaction import classify

    # Platform schemas resolve here as they do in the analysis; a catalog's own schema under a platform name wins
    # here and is refused by the analysis.
    bundle = platform_schema_bundle() | {name: schema.value for name, schema in catalog.schemas.items()}
    spec = document.get("spec") if isinstance(document.get("spec"), dict) else {}
    secret: list[str] = []
    unclassified: list[str] = []

    def check(path: str, schema: object, value: JsonValue, *, secrets_only: bool = False) -> None:
        # ``secrets_only`` for a schema the value may not fit: only a value it marks secret is counted.
        if not isinstance(schema, dict):
            return
        try:
            classify(schema, value, bundle)
        except _Failure as failure:
            if failure.code == "SECRET_VALUE":
                secret.append(path)
            elif not secrets_only:
                unclassified.append(path)
        except (RecursionError, ArithmeticError):
            if not secrets_only:
                unclassified.append(path)

    tools_of = _agent_tools

    def mocks_of(workflow: JsonObject, mocks: Mapping[str, JsonValue], *prefix: str) -> None:
        for key, value in mocks.items():
            if key.startswith("node:"):
                instance = key.removeprefix("node:")
                tools = _call_tools(workflow, instance, catalog)
                if tools is None:
                    check(_pointer(*prefix, key), pin_target(workflow, instance, catalog), value)
                # A mock of one call is checked for secrets by every schema of every tool, whether it fits that tool
                # or not, since the saved turns decide which tool the call runs.
                for schema in (schema for schemas in tools or [] for schema in schemas):
                    found = len(secret)
                    check(_pointer(*prefix, key), schema, value, secrets_only=True)
                    if len(secret) > found:
                        break
            elif key.startswith("action:"):
                # Like a live run: by the Action's output and its connector action's or worker task's output.
                for schema in _action_outputs(catalog, key.removeprefix("action:")):
                    found = len(secret) + len(unclassified)
                    check(_pointer(*prefix, key), schema, value)
                    if len(secret) + len(unclassified) > found:
                        break
            elif key.startswith("tool:"):
                activation, _, name = key.removeprefix("tool:").rpartition("/")
                tool = tools_of(step_at(workflow, activation)).get(name)
                returned = _tool_outputs(tool, catalog) if tool is not None else []
                for item in tool_values(value):
                    for schema in returned:
                        found = len(secret) + len(unclassified)
                        check(_pointer(*prefix, key), schema, item)
                        if len(secret) + len(unclassified) > found:
                            break

    check("/input", cast(JsonObject, spec).get("inputSchema"), request.input)
    mocks_of(document, request.mocks, "mocks")
    for reference, callee in request.workflows.items():
        called = (callees or {}).get(reference)
        if called is not None:
            mocks_of(called, callee.mocks, "workflows", reference, "mocks")
    for key, signal in request.scripts.signals.items():
        step = step_at(document, key)
        payload = signal.model_dump(mode="json", by_alias=True).get("payload")
        check(_pointer("scripts", "signals", key, "payload"), (step or {}).get("payloadSchema"), payload)
    for key, answer in request.scripts.humans.items():
        step = step_at(document, key)
        data = answer.model_dump(mode="json", by_alias=True).get("data")
        check(_pointer("scripts", "humans", key, "data"), (step or {}).get("formSchema"), data)
    profiles = cast(JsonObject, spec).get("llmProfiles")
    for key, entry in request.scripts.aiTurns.items():
        step = step_at(document, key) or {}
        profile = profiles.get(cast(str, step.get("profile"))) if isinstance(profiles, dict) else None
        schema = profile.get("outputSchema") if isinstance(profile, dict) else None
        tools = tools_of(step)
        for index, turn in enumerate(entry.turns):
            if "final" in turn:
                check(_pointer("scripts", "aiTurns", key, "turns", str(index), "final"), schema, turn["final"])
            for number, call in enumerate(cast(list[JsonValue], turn.get("toolCalls", []))):
                tool = tools.get(cast(str, call.get("name"))) if isinstance(call, dict) else None
                if tool is not None:
                    pointer = _pointer("scripts", "aiTurns", key, "turns", str(index), "toolCalls", str(number))
                    arguments = cast(JsonObject, call).get("arguments")
                    check(pointer + "/arguments", offline_input_schema(tool, catalog), arguments)
    return secret, unclassified


def _agent_tools(step: JsonObject | None) -> dict[str, JsonObject]:
    """An AI agent step's tools by name; none for any other step."""
    tools = step.get("tools") if step is not None and step.get("kind") == "agent" else None
    listed = tools if isinstance(tools, list) else []
    return {cast(str, tool["name"]): tool for tool in listed if isinstance(tool, dict) and "name" in tool}


def _action_outputs(catalog: CatalogSnapshot, reference: str) -> list[JsonObject]:
    """What a live run checks an Action's result with: the Action's output, and its connector action's or its worker
    task's output (``action_schemas``)."""
    resource = catalog.resources.get(("Action", reference))
    spec = resource.definition.value.get("spec") if resource is not None else None
    if not isinstance(spec, dict):
        return []
    schemas = [spec.get("outputSchema")]
    implementation = spec.get("implementation")
    if isinstance(implementation, dict) and implementation.get("kind") == "worker":
        task = f"{implementation.get('taskType')}@{implementation.get('taskVersion')}"
        capability = catalog.resources.get(("TaskCapability", task))
        schemas.append(capability.definition.value.get("outputSchema") if capability is not None else None)
    elif isinstance(implementation, dict) and isinstance(implementation.get("uses"), str):
        connector = catalog.resources.get(("Connector", cast(str, implementation["uses"])))
        connector_spec = connector.definition.value.get("spec") if connector is not None else None
        actions = connector_spec.get("actions") if isinstance(connector_spec, dict) else None
        name = implementation.get("action")
        action = actions.get(name) if isinstance(actions, dict) and isinstance(name, str) else None
        schemas.append(action.get("outputSchema") if isinstance(action, dict) else None)
    return [schema for schema in schemas if isinstance(schema, dict)]


def _tool_outputs(tool: JsonObject, catalog: CatalogSnapshot) -> list[JsonObject]:
    """Every schema a live run checks an AI agent tool's result with: what the tool returns and, for a tool that runs
    an Action, the schemas of that Action's result."""
    from firefly_weave.compiler.agent_tools import offline_output_schema

    schemas = [schema] if (schema := offline_output_schema(tool, catalog)) is not None else []
    action = tool.get("action")
    return schemas + (_action_outputs(catalog, action) if isinstance(action, str) else [])


def _call_tools(workflow: JsonObject, instance: str, catalog: CatalogSnapshot) -> list[list[JsonObject]] | None:
    """For a mock of one AI agent call (``node:support#1.1``), the schemas of each of the agent's tools, the way
    ``_tool_outputs`` reads them; None when ``instance`` isn't a call of an agent with tools."""
    from firefly_weave.compiler.test_data import step_at
    from firefly_weave.runtime.agents import parse_agent_key

    called = parse_agent_key(instance)
    tools = _agent_tools(step_at(workflow, instance)) if called is not None and called.call is not None else {}
    return [_tool_outputs(tool, catalog) for tool in tools.values()] or None


def _fits_a_tool(tools: list[list[JsonObject]], value: JsonValue, bundle: dict[str, JsonObject]) -> bool:
    """Whether a call's value fits, by its shape alone, every schema of one of the agent's tools at least."""
    from firefly_weave.compiler.schemas import payload_issues

    return any(schemas and all(payload_issues(schema, value, bundle) == [] for schema in schemas) for schemas in tools)


def secret_paths(document: JsonObject, request: SimulationRequest, catalog: CatalogSnapshot) -> list[str]:
    """Pointers into the request of values that can't be simulated: marked secret, or too large to classify."""
    secret, unclassified = classify_test_data(document, request, catalog)
    return [*secret, *unclassified]


def refuse_test_data(
    document: JsonObject,
    request: SimulationRequest,
    catalog: CatalogSnapshot,
    callees: Mapping[str, JsonObject] | None = None,
) -> None:
    """Refuses test data that holds a value marked secret, or one too large or too deep to check, by pointer."""
    secret, unclassified = classify_test_data(document, request, catalog, callees)
    if secret:
        raise SimulationRefused(
            422, "WV-SCHEMA-SECRET_VALUE", "Test data can't hold values marked secret; leave them out", secret
        )
    if unclassified:
        raise SimulationRefused(
            422,
            "WV-SCHEMA-RESOURCE_LIMIT",
            "This test data is too large or too deeply nested to check; make it smaller",
            unclassified,
        )


def _label(switch: JsonObject, branch: str) -> str:
    """A decision's path in plain words: "path 2" for its second case, "Otherwise" for its default."""
    if branch.startswith("case:") and branch[5:].isdigit():
        return f"path {int(branch[5:]) + 1}"
    return "Otherwise"


def _enclosing(document: JsonObject, step_id: str) -> list[tuple[JsonObject, Literal["switch", "errorPath"]]]:
    """The decisions holding a step, and the steps whose error path holds it, outermost first."""
    found: list[tuple[JsonObject, Literal["switch", "errorPath"]]] = []

    def walk(steps: object, outer: list[tuple[JsonObject, Literal["switch", "errorPath"]]]) -> bool:
        if not isinstance(steps, list):
            return False
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("id") == step_id:
                found.extend(outer)
                return True
            for branch in _branches(step):
                chain = (
                    [*outer, (step, "errorPath")]
                    if branch is step.get("errorPath")
                    else [*outer, (step, "switch")]
                    if step.get("kind") == "switch"
                    else outer
                )
                if walk(branch.get("steps"), chain):
                    return True
        return False

    spec = document.get("spec")
    walk(spec.get("steps") if isinstance(spec, dict) else None, [])
    return found


def _callees(request: SimulationRequest, catalog: CatalogSnapshot, stubs: Iterable[str]) -> dict[str, JsonObject]:
    """The called workflows test data has mocks for, as documents: a local draft's source, or the catalog's copy.

    Stubs (callees nobody has) are left out: their output comes from a pin on the call.
    """
    from firefly_weave.compiler.parser import ParseFailure, parse_source

    stubbed = set(stubs)
    documents: dict[str, JsonObject] = {}
    for reference, callee in request.workflows.items():
        if reference in stubbed:
            continue
        if callee.source is not None and callee.format is not None:
            try:
                documents[reference] = parse_source(callee.source, format=callee.format).value
            except ParseFailure:
                continue
        elif (resource := catalog.resources.get(("Workflow", reference))) is not None:
            documents[reference] = resource.definition.value
    return documents


def _called(
    artifact: Any, request: SimulationRequest, documents: Mapping[str, JsonObject], catalog: CatalogSnapshot
) -> dict[str, Any]:
    """Compiled called workflows the run reaches, with the mocks test data holds for their steps."""
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.operations.debug.models import CalledWorkflow

    called: dict[str, Any] = {}
    pending = [artifact]
    while pending:
        current = pending.pop()
        for dependency in _dependencies(current):
            reference = dependency.reference
            if dependency.kind != "Workflow" or reference in called or reference not in documents:
                continue
            result = compile_source(documents[reference], format="object", catalog=catalog)
            if not result.ok or result.artifact is None:
                continue
            called[reference] = CalledWorkflow(
                artifact=json.loads(result.artifact.to_bytes()),
                mocks=dict(request.workflows[reference].mocks),
            )
            pending.append(result.artifact)
    return called


def _dependencies(artifact: Any) -> list[Any]:
    from firefly_weave.runtime.kernel import prepare

    return list(prepare(artifact).ir.dependencies)


def _in_slice[V](document: JsonObject, whole: JsonObject, mocks: Mapping[str, V]) -> dict[str, V]:
    """The mocks (or failures) for steps the compiled document runs: a later step's pin never blocks executing an
    earlier one.

    A key that names no step (or action) anywhere in the workflow stays, so the simulator still refuses it.
    """
    from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of
    from firefly_weave.contracts.transactions import PHASE_SEGMENTS

    def ids(workflow: JsonObject) -> set[str]:
        spec = workflow.get("spec")
        return {cast(str, step.get("id")) for step in steps_of(spec.get("steps") if isinstance(spec, dict) else None)}

    def actions(workflow: JsonObject) -> set[str]:
        # The actions its steps run, and those that confirm, release or undo a step.
        spec = workflow.get("spec")
        return {
            cast(str, operation.get("uses"))
            for step in steps_of(spec.get("steps") if isinstance(spec, dict) else None)
            for operation in (step, *(step.get(phase) for phase in PHASE_SEGMENTS))
            if isinstance(operation, dict) and operation.get("kind") == "action"
        }

    kept_ids, all_ids = ids(document), ids(whole)
    kept_actions, all_actions = actions(document), actions(whole)
    out: dict[str, V] = {}
    for key, value in mocks.items():
        if key.startswith("node:"):
            try:
                step = node_of(key.removeprefix("node:"))
            except InvalidInstanceKey:
                step = key.removeprefix("node:")
            if step in all_ids and step not in kept_ids:
                continue
        elif key.startswith("action:"):
            reference = key.removeprefix("action:")
            if reference in all_actions and reference not in kept_actions:
                continue
        elif key.startswith(("agent:", "tool:")):
            # An AI agent's saved turns or tool mock, by the agent they belong to.
            activation = key.partition(":")[2].rpartition("/")[0] if key.startswith("tool:") else key[6:]
            try:
                step = node_of(activation)
            except InvalidInstanceKey:
                step = activation
            if step in all_ids and step not in kept_ids:
                continue
        out[key] = value
    return out


def simulator_for(
    request: SimulationRequest,
    project: CatalogSnapshot | None,
    document: JsonObject,
    *,
    whole: JsonObject,
    sliced: bool,
) -> tuple[CompileSummary, Simulator | None, set[str]]:
    """Compile ``document`` (the workflow, or its slice) for a simulation and start it with the request's test data.

    Test data is checked against the whole workflow before compiling (secrets, and values too large to check);
    mocks for steps the slice doesn't run, and mocks that no longer fit, are left out before the simulator sees
    them (the misfits are returned); called workflows run with the mocks test data holds for their steps; AI tasks
    replay their saved turns; simulated failures apply like mocks, to the steps the document runs. The simulator is
    None when the workflow doesn't compile. Raises ``SimulationRefused``, ``CatalogConflict`` or ``DebugError``.
    """
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.operations.debug.ai_turns import with_ai_turns
    from firefly_weave.operations.debug.models import ScriptedError
    from firefly_weave.runtime.kernel import KernelError
    from firefly_weave.studio.local_contracts import CompileSummary

    catalog, stubs = simulation_catalog(document, request.definitions, project, stub_unresolved=request.stubUnresolved)
    callees = _callees(request, catalog, stubs)
    refuse_test_data(whole, request, catalog, callees)
    result = compile_source(document, format="object", catalog=catalog)
    summary = CompileSummary(
        ok=result.ok,
        sliced=sliced,
        artifactDigest=result.artifact.digest if result.artifact is not None else None,
        diagnostics=list(result.diagnostics),
        stubs=stubs,
    )
    if not result.ok or result.artifact is None:
        return summary.model_copy(update={"ok": False}), None, set()
    artifact = result.artifact
    misfits = misfitting_mocks(whole, request.mocks, catalog)
    ai_turns = {key: entry.model_dump(mode="json", by_alias=True) for key, entry in request.scripts.aiTurns.items()}
    kept = _in_slice(document, whole, {k: v for k, v in request.mocks.items() if k not in misfits})
    mocks = with_ai_turns(artifact, kept, ai_turns)
    workflows = _called(artifact, request, callees, catalog)
    errors = {
        key: ScriptedError(code=failure.code, outcome=failure.outcome, attempts=failure.attempts, effect=failure.effect)
        for key, failure in _in_slice(document, whole, request.errors).items()
    }
    try:
        simulator = _watching_simulator()(
            artifact, mocks=mocks, input=request.input, now=request.now, workflows=workflows, errors=errors
        )
    except KernelError as error:
        # The test input doesn't fit the workflow's input fields: said with its pointer, never its value.
        raise SimulationRefused(422, str(error), "The test input doesn't fit the input fields", ["/input"]) from None
    return summary, simulator, misfits


@functools.cache
def _watching_simulator() -> Any:
    """The simulator the host runs, which keeps the last record of each transaction of each run.

    The runtime deletes a transaction's record from the run's state when its loop iteration is released, in the same
    event that ends it, and reports the final record nowhere else. This is the one place that works around it: once
    the runtime reports the final record, the debug view lists it and the host can run a plain ``Simulator``.
    """
    from firefly_weave.operations.debug.simulator import Simulator

    class WatchingSimulator(Simulator):
        def __init__(self, artifact: CompiledArtifact, **options: Any) -> None:
            super().__init__(artifact, **options)
            self._records: dict[str, dict[str, TransactionRecord]] = {}

        def prepared_of(self, frame: str) -> PreparedWorkflow:
            """The workflow a run executes: the simulated one ("") or a called run's."""
            return self._workflow(frame).prepared

        def transactions_of(self, frame: str) -> dict[str, TransactionRecord]:
            """The last record of each transaction of a run, by instance key, in the order they opened."""
            run = self._run(frame)
            state = run.cursor.result.state if run.cursor is not None else cast("RunState", run.state)
            return {**self._records.get(frame, {}), **state.transactions}

        def _step_run(self, key: str, run: Any, event: RuntimeEvent | None) -> bool:
            moved = super()._step_run(key, run, event)
            state = run.cursor.result.state if run.cursor is not None else run.state
            self._records.setdefault(key, {}).update(state.transactions)
            return moved

    return WatchingSimulator


class _Stepper:
    """A simulator that moves one boundary at a time under ``_drive``, noting where it is before each move."""

    def __init__(self, simulator: Any) -> None:
        self.simulator = simulator
        self.here: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.simulator, name)

    def continue_until_breakpoint(self) -> Any:
        from firefly_weave.operations.debug.models import DebugCommand
        from firefly_weave.operations.debug.simulator import TERMINAL

        view = self.simulator.inspect()
        for _ in range(ROUNDS):
            before = (view.status, list(view.current_nodes), len(self.simulator.data.boundaries))
            self.here = list(view.current_nodes)
            view = self.simulator.command(DebugCommand(kind="next"))
            if view.status in TERMINAL or view.diagnostics or self.simulator.data.paused is not None:
                return view
            if (view.status, list(view.current_nodes), len(self.simulator.data.boundaries)) == before:
                return view
        return view


def stuck_at(simulator: Any, scripts: Any = None) -> tuple[str, Any]:
    """Where a run stops on something the simulator can't carry on with, and the run up to there.

    ``simulator`` is a fresh one (the failed one's state is unknown). It replays as Execute does (scripted signals
    and people answered, waits advanced) one boundary at a time; the step that raises names the place.
    """
    from types import SimpleNamespace

    from firefly_weave.compiler.ir import HumanTaskNode, SignalNode
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.operations.debug.simulator import TERMINAL

    stepper = _Stepper(simulator)
    try:
        _drive(stepper, scripts or SimpleNamespace(signals={}, humans={}), SignalNode, HumanTaskNode, TERMINAL)
    except Exception:  # noqa: BLE001 - this boundary is where it stops
        return (f"step {node_of(stepper.here[0])}" if stepper.here else "the current step"), simulator
    return "the current step", simulator


def _unsettled(data: DebugState) -> str | None:
    """The first transaction that still holds work, in words, when the run didn't end or pause; None otherwise."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.operations.debug.simulator import TERMINAL
    from firefly_weave.runtime.transactions import UNSETTLED

    if data.paused is not None or data.state is None or data.state.status in TERMINAL:
        return None
    runs = [("", data.state), *((f" of {f.workflow.rpartition('@')[0]}", f.state) for f in data.frames if f.state)]
    for within, state in runs:
        for key, record in state.transactions.items():
            if record.status in UNSETTLED:
                return f"transaction {node_of(key)}{within}"
    return None


def unsimulated(place: str) -> str:
    """The sentence for a run the simulator couldn't carry on with."""
    return f"Studio couldn't simulate {place}, so the simulated run stopped there."


def execute(request: ExecuteRequest, project: CatalogSnapshot | None) -> ExecuteResponse:
    """Simulate the target of an Execute request; the answer is ``ExecuteResponse``.

    Raises ``SimulationRefused`` for a request the host must refuse, and ``CatalogConflict`` for a local definition
    that conflicts with the catalog. Nothing here reads the network, the clock or files.
    """
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.compiler.ir import HumanTaskNode, SignalNode
    from firefly_weave.compiler.parser import ParseFailure, parse_source
    from firefly_weave.contracts.diagnostics import Diagnostic
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.contracts.transactions import IN_DOUBT_CODES, TRANSACTION_MESSAGES
    from firefly_weave.operations.debug.models import DebugError
    from firefly_weave.operations.debug.simulator import TERMINAL, first_instance
    from firefly_weave.operations.debug.transactions import transaction_views
    from firefly_weave.runtime.kernel import KernelError, prepare
    from firefly_weave.runtime.models import RunState
    from firefly_weave.studio.local_contracts import (
        Blocked,
        CompileSummary,
        ExecuteResponse,
        FrameView,
        LoopProgress,
        PhaseEntry,
        SelectedScope,
        TraceEntry,
        TransactionTrace,
    )

    if not request.scripts.autoAdvanceWaits:
        raise SimulationRefused(422, "WV-STUDIO-REQUEST", "Execute advances waits; step through a session instead")
    target = request.target
    try:
        document: JsonObject = parse_source(request.source, format=request.format).value
    except ParseFailure as failure:
        unparsed = CompileSummary(ok=False, sliced=False, diagnostics=list(failure.diagnostics), stubs=[])
        return ExecuteResponse(status="failed", compile=unparsed, trace=[])
    whole = document
    sliced = False
    if target.mode != "workflow":
        part = slice_for_target(document, cast(str, target.stepId))
        if part is None:
            raise SimulationRefused(422, "WV-STUDIO-REQUEST", "That step isn't in this workflow")
        document, sliced = part, True
    scripts = request.scripts
    summary: CompileSummary | None = None
    unsimulated_at: str | None = None
    try:
        summary, simulator, misfits = simulator_for(request, project, document, whole=whole, sliced=sliced)
        if simulator is None:
            return ExecuteResponse(status="failed", compile=summary, trace=[])
        if target.mode == "before":
            simulator.set_breakpoints({target.instanceKey or first_instance(simulator.ir, cast(str, target.stepId))})
        try:
            blocked = _drive(simulator, scripts, SignalNode, HumanTaskNode, TERMINAL)
        except KernelError as error:
            # A step the runtime refused (a condition that isn't true or false, say): the run fails there.
            stopped = Diagnostic(
                code=str(error), severity="error", stage="evaluation", path="", message="The simulated run stopped."
            )
            return ExecuteResponse(status="failed", compile=summary, trace=[], diagnostics=[stopped])
        except (DebugError, SimulationRefused):
            raise
        except Exception as error:  # noqa: BLE001 - never a 500: the run fails where the simulator stopped
            _, fresh, _ = simulator_for(request, project, document, whole=whole, sliced=sliced)
            if fresh is None:
                raise
            place, simulator = stuck_at(fresh, scripts)
            # The kind of failure and where, never its message (it can hold values).
            logger.debug("Studio couldn't simulate %s: %s", place, type(error).__name__)
            unsimulated_at = place
            blocked = None
    except DebugError as error:
        if error.code == "WV-DEBUG-LIMIT" and summary is not None:
            return ExecuteResponse(status="limit", compile=summary, trace=[])
        raise SimulationRefused(422, error.code, "The simulation couldn't use this test data") from None
    view = simulator.inspect()
    data = simulator.data
    steps = {step.get("id") for step in steps_of(cast(JsonObject, whole["spec"]).get("steps"))}
    trace: list[TraceEntry] = []
    seen: set[tuple[str | None, str]] = set()
    # The called workflows that ran here, by frame key; a detached call without its workflow ran nothing. A called
    # confirm, release or undo is one of the phases: its own steps stay out of the trace, and its run out of the frames.
    frames = {frame.key: frame for frame in view.frames if frame.status != "not_simulated"}
    shown = {key: frame for key, frame in frames.items() if _called_phase(key) is None}
    phases: list[JsonObject] = []
    recorded = iter(view.events)
    # A run held on an incident (a step the runtime refused) stops there until a person ends it: it failed there. So
    # does a run left waiting on a called run held that way.
    held = _incident(view.status, view.variables)
    held_frames = {key: code for key, frame in frames.items() if (code := _incident(frame.status, frame.variables))}
    statuses: dict[str, Literal["running", "waiting", "completed", "failed"]] = {
        key: "failed" if key in held_frames else _frame_status(frame.status) for key, frame in frames.items()
    }
    waits_on_held = bool(held_frames) and blocked is None and view.status == "waiting"
    on_hold = held is not None or waits_on_held
    # A run that timed out (a deadline passed) or was cancelled stopped too: it failed where it waited.
    timed_out = view.status in {"timed_out", "cancelled"}
    failed = (view.status == "failed" or timed_out or on_hold) and not view.diagnostics
    if not failed and blocked is None and not view.diagnostics:
        # An answer never says completed while a transaction has work left, unless Execute stopped at its target.
        unsimulated_at = unsimulated_at or _unsettled(data)

    @functools.cache
    def handlers(frame: str | None) -> dict[str, Any]:
        # The steps of the simulated workflow, or of the called workflow a frame runs, that handle their failures.
        if frame is None:
            return _error_handlers(simulator.ir)
        called = data.workflows.get(frames[frame].workflow) if frame in frames else None
        return _error_handlers(prepare(import_artifact(called.artifact)).ir) if called and called.artifact else {}

    for boundary in data.boundaries:
        if boundary.kind == "event" and boundary.frame is None:
            # The simulated workflow's event boundaries are its recorded events, in order.
            event = next(recorded, None)
            result = event.data.get("node_id") if event is not None else None
            if isinstance(result, str) and _phase_of(result) is not None:
                outcome: Literal["completed", "failed"] = "completed" if result in data.outputs else "failed"
                phases.append(_phase_entry(result, len(trace) - 1, outcome, data))
        if boundary.kind != "node" or boundary.node_id is None:
            continue
        frame, key = boundary.frame, boundary.node_id
        # A step inside a called workflow comes with the frame it ran in; its key is its own workflow's. The nodes a
        # compiled workflow adds (@start, @end, @join…) are no one's steps.
        step_of_a_run = node_of(key) in steps if frame is None else frame in shown and not key.startswith("@")
        if not step_of_a_run or (frame, key) in seen:
            continue
        seen.add((frame, key))
        path = key if frame is None else f"{frame}/{key}"
        active = view.active_nodes if frame is None else frames[frame].active_nodes
        reached = "completed" if path in data.outputs else "waiting" if key in active else "running"
        entry: dict[str, Any] = {
            "index": len(trace),
            "nodeId": node_of(key),
            "instanceKey": "" if key == node_of(key) else key,
            "kind": cast(str, boundary.node_kind),
            "status": reached,
        }
        if frame is not None:
            entry["frame"] = frame
        if path in data.inputs:
            entry["input"] = data.inputs[path]
        if path in data.outputs:
            entry["output"] = data.outputs[path]
        if path in data.failures:
            entry |= _handled_entry(handlers(frame), key, data.failures[path], data.outputs, frame)
        trace.append(TraceEntry.model_validate(entry))
    phases.extend(_unsettled_phases(view.variables, data, {cast(str, entry["instanceKey"]) for entry in phases}, trace))
    records = cast(Any, simulator).transactions_of
    kept = RunState(input=None, transactions=records(""))
    transactions = [_transaction_entry(record) for record in transaction_views(kept)]
    if _too_many(transactions, phases):
        return ExecuteResponse(status="limit", compile=summary, trace=[])
    _refuse_secret_phases(data.artifact, phases, view.variables)
    if unsimulated_at is not None:
        failed = True
    # Where each run stopped: the simulated workflow's last step, and the last step of each called run that failed.
    ended: set[str | None] = {key for key, status in statuses.items() if status == "failed"}
    if failed:
        ended.add(None)
    # The failure each step of the simulated workflow reported last, and the actions its steps use.
    reported = _reported(view.events)
    uses = {
        cast(str, step["id"]): cast(str, step["uses"])
        for step in steps_of(cast(JsonObject, whole["spec"]).get("steps"))
        if isinstance(step.get("uses"), str)
    }
    for frame in [None, *shown]:
        shapes = cast(Any, simulator).prepared_of(frame or "").transactions
        stopped_by_transaction = False
        handled: set[str | None] = set()
        if shapes is not None:
            # A run with transactions: what failed inside them, whatever settled after it.
            handled = {_entry_key(entry) for entry in trace if entry.frame == frame and entry.handled is not None}
            own_errors, own_uses = (request.errors, uses) if frame is None else ({}, {})
            called = next((item for item in data.frames if item.key == frame), None)
            reports = reported if called is None else _reported(called.events)
            variables = view.variables if frame is None else frames[frame].variables
            causes = _named_failures(records(frame or ""), variables)
            stopped_by_transaction = (held if frame is None else held_frames.get(frame)) in IN_DOUBT_CODES
            for index, update in _transaction_failures(trace, frame, shapes, causes, reports, own_errors, own_uses):
                trace[index] = trace[index].model_copy(update=update)
                stopped_by_transaction |= shapes.owner_key(_entry_key(trace[index])) not in handled
        last_of_run = next((i for i in reversed(range(len(trace))) if trace[i].frame == frame), None)
        if frame not in ended or last_of_run is None or stopped_by_transaction:
            continue
        stopping = trace[last_of_run]
        key = _entry_key(stopping)
        # A step left unfinished by a transaction whose failure was handled didn't stop the run.
        if stopping.status == "completed" or (shapes is not None and shapes.owner_key(key) in handled):
            continue
        update = {"status": "failed"}
        if frame is None and stopping.handled is None:
            stopped = _stopping_error(key, stopping.kind, request.errors, uses, reported, held)
            if stopped is not None:
                update["error"] = stopped
        trace[last_of_run] = stopping.model_copy(update=update)
    own = [entry for entry in trace if entry.frame is None]
    extra: list[Diagnostic] = []
    if timed_out:
        at = own[-1] if own and own[-1].status == "failed" else None
        node = next((n for n in simulator.ir.graph.nodes if at is not None and n.id == at.nodeId), None)
        if view.status == "cancelled":
            words = "The simulated run was cancelled."
        elif isinstance(node, SignalNode):
            words = f"The deadline of {node.name} passed before the signal arrived."
        elif isinstance(node, HumanTaskNode):
            words = f"The task {node.id} expired before anyone decided."
        else:
            words = "A deadline passed, so the simulated run stopped."
        extra.append(
            Diagnostic(
                code="WV-DEBUG-TIMED-OUT" if view.status == "timed_out" else "WV-DEBUG-CANCELLED",
                severity="error",
                stage="evaluation",
                path="",
                message=words,
            )
        )
    if view.status == "failed" and view.variables.get("incident") == "WV-TXN-ROLLED-BACK":
        # The run failed because its transaction was undone, and nothing it did stays applied.
        words = TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]
        extra.append(
            Diagnostic(code="WV-TXN-ROLLED-BACK", severity="error", stage="evaluation", path="", message=words)
        )
    for code in [*([held] if held is not None else []), *held_frames.values()]:
        words = IN_DOUBT if code in IN_DOUBT_CODES else _incident_words(code)
        extra.append(Diagnostic(code=code, severity="error", stage="evaluation", path="", message=words))
    if unsimulated_at is not None:
        extra.append(
            Diagnostic(
                code="WV-STUDIO-SIMULATION",
                severity="error",
                stage="evaluation",
                path="",
                message=unsimulated(unsimulated_at),
            )
        )
    missing = next((d for d in view.diagnostics if d.code == "WV-DEBUG-MISSING_MOCK"), None)
    if blocked is None and missing is not None:
        last = next(reversed(data.inputs), None) if data.inputs else None
        if last is not None and last not in data.outputs:
            from firefly_weave.runtime.agents import parse_agent_key

            # Inside a called workflow the key is the frame's, then the step's own: "notify/enrich".
            inside, _, last = last.rpartition("/")
            step = node_of(last)
            called = parse_agent_key(last)
            named = _phase_name(last)
            what = f"The {named}" if named is not None else f"Step {step}"
            workflow = frames[inside].workflow.rpartition("@")[0] if inside in frames else None
            calling = _called_phase(inside)
            if calling is not None:
                # Inside a called confirm, release or undo: it is answered whole, where it is called.
                inside, last = calling
                message = f"The {_phase_name(last)} needs test data: step {step} of {workflow} has none."
                step = node_of(last)
            elif inside:
                # Its test data goes under the called workflow's mocks, which apply wherever that workflow runs.
                message = f"{what} {f'in {workflow} ' if workflow else ''}needs test data."
            elif named is not None and {"node:" + last, f"node:{step}#{last.partition('#')[2]}"} & misfits:
                # A confirm, release or undo is answered at its own keys, never at its step's.
                message = f"The pinned output of the {named} doesn't fit its output fields. Edit it, or unpin it."
            elif "node:" + last in misfits and called is not None and called.call is not None:
                # A mock of one agent call is a tool's result, so it misfits the agent's tools, not the agent's pin.
                message = f"The pinned result of call {last} fits none of the tools of {step}. Edit it, or unpin it."
            elif named is None and {"node:" + last, "node:" + step} & misfits:
                message = f"The pinned output of {step} doesn't fit its output fields. Edit it, or unpin it."
            else:
                message = f"{what} needs test data."
            blocked = Blocked(
                reason="missing_mock",
                nodeId=step,
                instanceKey="" if last == step else last,
                message=message,
                **({"frame": inside} if inside else {}),
            )
    if blocked is None and target.mode != "workflow" and view.status not in {"paused"} and not view.diagnostics:
        step = cast(str, target.stepId)
        if not any(entry.nodeId == step and entry.frame is None for entry in trace):
            for switch, holds in reversed(_enclosing(whole, step)):
                if holds == "errorPath":
                    # The step's error path runs only after it fails; one that only succeeded never ran it.
                    owner = cast(str, switch["id"])
                    ran = [entry for entry in own if entry.nodeId == owner]
                    if ran and all(entry.status == "completed" for entry in ran):
                        blocked = Blocked(
                            reason="path",
                            nodeId=step,
                            instanceKey="",
                            message=f"With this test data, {owner} succeeds, so {step} isn't reached.",
                        )
                        break
                    continue
                taken = next(
                    (
                        cast(JsonObject, b)
                        for k, b in cast(JsonObject, view.variables.get("branches", {})).items()
                        if cast(JsonObject, b).get("owner") == switch["id"]
                    ),
                    None,
                )
                if taken is not None:
                    blocked = Blocked(
                        reason="path",
                        nodeId=step,
                        instanceKey="",
                        message=(
                            f"With this test data, {switch['id']} takes {_label(switch, cast(str, taken['name']))},"
                            f" so {step} isn't reached."
                        ),
                    )
                    break
    if blocked is not None:
        status: Literal["completed", "blocked", "failed", "limit"] = "blocked"
    elif view.diagnostics or view.status == "failed" or timed_out or on_hold or unsimulated_at is not None:
        status = "failed"
    else:
        status = "completed"
    # Each left out when there are none, so other answers keep their bytes.
    listed: dict[str, Any] = {}
    if shown:
        listed["frames"] = [
            FrameView(key=key, workflow=frame.workflow, callNode=frame.call_node, status=statuses[key])
            for key, frame in shown.items()
        ]
    if transactions:
        listed["transactions"] = [TransactionTrace.model_validate(entry) for entry in transactions]
    if phases:
        listed["phases"] = [PhaseEntry.model_validate(entry) for entry in phases]
    return ExecuteResponse(
        status=status,
        compile=summary,
        trace=trace,
        loops=[
            LoopProgress(
                nodeId=loop.node_id,
                instanceKey=loop.instance_key,
                count=loop.count,
                completed=loop.completed,
                running=loop.running,
                nextIndex=loop.next_index,
                status="completed"
                if loop.status == "joined"
                else "failed"
                if loop.status == "terminated"
                else "running",
            )
            for loop in view.loops
        ],
        selectedScope=(
            SelectedScope.model_validate(view.selected_scope.model_dump(mode="json"))
            if view.selected_scope is not None
            else None
        ),
        blocked=blocked,
        **listed,
        # Only what Studio reads: the paths decisions took. The run's values are in the trace already.
        variables={"branches": view.variables["branches"]} if "branches" in view.variables else {},
        diagnostics=[
            *(d for d in view.diagnostics if d.code != "WV-DEBUG-MISSING_MOCK" or blocked is None),
            *extra,
        ],
    )


# What each phase of a step in a transaction does, as Studio names it.
_PHASE_NAMES: Mapping[UndoPhase, str] = {"confirm": "confirm", "cancel": "release", "compensate": "undo"}
IN_DOUBT = "A transaction is in doubt, so the simulated run stopped."


def _phase_of(key: str) -> UndoPhase | None:
    """The phase an instance key names (``hold#confirm``), or None for a step's own key."""
    from firefly_weave.contracts.instance_keys import InvalidInstanceKey
    from firefly_weave.contracts.transactions import split_phase

    try:
        found = split_phase(key)
    except InvalidInstanceKey:
        return None
    return found[1] if found is not None else None


def _phase_name(key: str) -> str | None:
    """What a confirm, release or undo is called ("undo of add"), or None for a step's own key."""
    from firefly_weave.contracts.instance_keys import node_of

    phase = _phase_of(key)
    return f"{_PHASE_NAMES[phase]} of {node_of(key)}" if phase is not None else None


def _called_phase(frame: str) -> tuple[str, str] | None:
    """The called confirm, release or undo a frame runs in: the frame that called it ("" for the simulated workflow)
    and its key there. None for a frame outside of one."""
    parts = frame.split("/")
    for index, part in enumerate(parts):
        if _phase_of(part) is not None:
            return "/".join(parts[:index]), part
    return None


def _phase_entry(
    key: str, after: int, status: Literal["completed", "failed", "waiting"], data: DebugState
) -> JsonObject:
    """One confirm, release or undo in the answer's words, with what it received and returned."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.contracts.transactions import PHASE_SEGMENTS

    entry: JsonObject = {
        "afterIndex": after,
        "nodeId": node_of(key),
        "instanceKey": key,
        "phase": PHASE_SEGMENTS[cast("UndoPhase", _phase_of(key))],
        "status": status,
    }
    if key in data.inputs:
        entry["input"] = data.inputs[key]
    if key in data.outputs:
        entry["output"] = data.outputs[key]
    return entry


def _unsettled_phases(
    variables: Mapping[str, Any], data: DebugState, settled: set[str], trace: Sequence[object]
) -> Iterator[JsonObject]:
    """The phases no result was recorded for: those that failed before they were sent (their input couldn't be built,
    so their transaction is in doubt), then those still waiting for one."""
    after = len(trace) - 1
    stuck = [cast(str, incident["node_id"]) for incident in variables.get("incidents", {}).values()]
    for key in stuck:
        if _phase_of(key) is not None and key not in settled and key not in data.inputs:
            yield _phase_entry(key, after, "failed", data)
    for key in data.inputs:
        if "/" not in key and _phase_of(key) is not None and key not in settled:
            yield _phase_entry(key, after, "waiting", data)


def _transaction_entry(record: TransactionDebugView) -> JsonObject:
    """One transaction in the answer's words: keys, states, codes and counts, never its business key."""
    from firefly_weave.contracts.studio_documents import ERROR_CODE_PATTERN

    entry: JsonObject = {"instanceKey": record.instance_key, "nodeId": record.node_id, "state": record.state}
    for name, cause in (("cause", record.cause), ("pendingAbort", record.pending_abort)):
        # A fail step's code is any name its author gave it; only codes spelled as codes are passed on.
        if cause is not None and re.fullmatch(ERROR_CODE_PATTERN[1:-1], cause.code) is not None:
            entry[name] = {"code": cause.code, "instanceKey": cause.instance_key, "effect": cause.effect}
    if record.incident_key is not None:
        entry["incidentKey"] = record.incident_key
    if record.reconciled_as is not None:
        entry["reconciledAs"] = record.reconciled_as
    entry["participants"] = [
        {
            "nodeId": item.node_id,
            "instanceKey": item.instance_key,
            "mode": item.mode,
            "status": item.status,
            "effect": item.effect,
            **({"attempts": item.attempts} if item.attempts is not None else {}),
        }
        for item in record.participants
    ]
    return entry


def _too_many(transactions: list[JsonObject], phases: list[JsonObject]) -> bool:
    """Whether a run did more to its transactions than an answer holds: entries of either kind, or bytes of both."""
    from firefly_weave.contracts.studio_documents import TEST_DATA_MAX_BYTES
    from firefly_weave.studio import local_contracts

    if max(len(transactions), len(phases)) > local_contracts.MAX_TRANSACTION_ENTRIES:
        return True
    written = json.dumps([transactions, phases], ensure_ascii=False, separators=(",", ":"))
    return len(written.encode()) > TEST_DATA_MAX_BYTES


def secret_phases(artifact: JsonObject, phases: Iterable[Mapping[str, Any]]) -> list[str]:
    """Pointers (``/phases/add#undo/input``) of what a confirm, release or undo received or returned that its own
    operation marks secret, or that can't be classified. An undo's input derives from what the steps before it
    returned, so it is checked here too.
    """
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.operations.debug.transactions import phase_target
    from firefly_weave.operations.redaction import classify
    from firefly_weave.runtime.kernel import prepare

    prepared = prepare(import_artifact(artifact))
    bundle = {d.reference: d.document for d in prepared.ir.dependencies if d.kind == "Schema"}
    found: list[str] = []
    for entry in phases:
        key = cast(str, entry["instanceKey"])
        target = phase_target(prepared, key)
        for direction in ("input", "output"):
            if target is None or direction not in entry:
                continue
            for schema in _phase_schemas(prepared.ir, target, direction):
                try:
                    classify(schema, entry[direction], bundle)
                except (_Failure, RecursionError, ArithmeticError):
                    found.append(_pointer("phases", key, direction))
                    break
    return found


def _phase_schemas(ir: WorkflowIR, target: PhaseTarget, direction: Literal["input", "output"]) -> list[JsonObject]:
    """What a live run checks a phase's input or output with: its action's schemas, or its called workflow's."""
    from firefly_weave.contracts.definitions import ActionDefinition, load_definition
    from firefly_weave.runtime.kernel import action_schemas

    kind = "Action" if target.kind == "action" else "Workflow"
    document = next(d.document for d in ir.dependencies if d.kind == kind and d.reference == target.reference)
    if kind == "Action":
        return action_schemas(ir, cast(ActionDefinition, load_definition(document)), direction)
    return [cast(JsonObject, cast(JsonObject, document["spec"])[direction + "Schema"])]


def _refuse_secret_phases(
    artifact: JsonObject, phases: Sequence[Mapping[str, Any]], variables: Mapping[str, Any]
) -> None:
    """Refuses an answer about a confirm, release or undo that received or returned a value marked secret, by pointer.

    The runtime never sends such an input or accepts such an output: it puts the transaction in doubt with that code.
    """
    found = secret_phases(artifact, phases) if phases else []
    entries = {cast(str, entry["instanceKey"]): entry for entry in phases}
    for incident in variables.get("incidents", {}).values():
        key = cast(str, incident["node_id"])
        if incident.get("origin_code") == "WV-SCHEMA-SECRET_VALUE" and _phase_of(key) is not None:
            found.append(_pointer("phases", key, "output" if "input" in entries.get(key, {}) else "input"))
    if found:
        raise SimulationRefused(
            422,
            "WV-SCHEMA-SECRET_VALUE",
            "A confirm, release or undo can't take or return values marked secret; leave them out",
            dict.fromkeys(found),
        )


def _error_handlers(ir: WorkflowIR) -> dict[str, Any]:
    """The steps of a compiled workflow whose onError setting handles their failures, by step ID."""
    from firefly_weave.compiler.ir import HandlesErrors

    return {node.id: node for node in ir.graph.nodes if isinstance(node, HandlesErrors) and node.on_error is not None}


def _handled_entry(
    handlers: Mapping[str, Any], key: str, error: JsonObject, outputs: Mapping[str, JsonValue], frame: str | None
) -> dict[str, Any]:
    """What a trace entry says about a failure the step's setting handled: failed, its error and the setting, and
    with an error path, the path's output once the path ended (recorded at the path's join, never at the step)."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.contracts.step_errors import StepError

    step = node_of(key)
    # Validated as the kernel builds it: a called run's ID arrives as text.
    entry: dict[str, Any] = {"status": "failed", "error": StepError.model_validate(error, strict=False)}
    handler = handlers.get(step)
    if handler is None:
        return entry
    entry["handled"] = handler.on_error
    if handler.on_error == "errorOutput" and handler.error_join is not None:
        # The join is an instance of the same loops as the step: its key ends like the step's.
        joined = handler.error_join + key[len(step) :]
        joined = joined if frame is None else f"{frame}/{joined}"
        if joined in outputs:
            entry["output"] = outputs[joined]
    return entry


def _incident_words(code: str) -> str:
    """An incident's meaning in plain words, as the simulated run's diagnostics give it."""
    from firefly_weave.operations.incident_codes import INCIDENT_CODES

    known = INCIDENT_CODES.get(code)
    return known.meaning if known is not None else "The simulated run stopped."


def _error_object(
    key: str, code: Any, outcome: Any, attempts: Any, child_run_id: Any = None, message: str | None = None
) -> Any:
    """The error object of a failure of the step instance ``key``, or None when its parts don't make one."""
    from firefly_weave.contracts.step_errors import StepError, step_error

    try:
        return StepError.model_validate(
            step_error(code, outcome, attempts, key, message=message, child_run_id=child_run_id), strict=False
        )
    except (TypeError, ValueError):
        return None


def _entry_key(entry: TraceEntry) -> str:
    """The instance key of a trace entry in its own run."""
    return entry.instanceKey or entry.nodeId


def _reported(events: Iterable[RuntimeEvent]) -> dict[str, Mapping[str, Any]]:
    """The failure each step instance of a run reported last."""
    return {cast(str, event.data.get("node_id")): event.data for event in events if event.type == "step_failed"}


def _named_failures(records: Mapping[str, TransactionRecord], variables: Mapping[str, Any]) -> dict[str, str]:
    """The code of each failure a run's transactions name, by the step instance it happened at: what they are undone
    for, and what put them in doubt. A transaction itself failed only when its reservations expired: one that is
    undone because its run ends didn't."""
    named: dict[str, str] = {}
    for incident in variables.get("incidents", {}).values():
        named[incident["node_id"]] = incident.get("origin_code") or incident["code"]
    for key, record in records.items():
        for cause in (record.pending_abort, record.cause):
            if cause is not None and (cause.instance_key != key or cause.code == "WV-TXN-HOLD-EXPIRED"):
                named[cause.instance_key] = cause.code
    return named


def _transaction_failures(
    trace: Sequence[TraceEntry],
    frame: str | None,
    shapes: TransactionShapes,
    named: Mapping[str, str],
    reported: Mapping[str, Mapping[str, Any]],
    errors: Mapping[str, Any],
    uses: Mapping[str, str],
) -> Iterator[tuple[int, dict[str, Any]]]:
    """The entries of a run that failed inside its transactions, and the transactions whose reservations expired,
    with their status and error. The other steps of a transaction settle before it does, so a failed one is often not
    the last of the trace: it is an entry without an output that reported a failure, or that a transaction names."""
    for index, entry in enumerate(trace):
        key = _entry_key(entry)
        if entry.frame != frame or entry.status in {"completed", "failed"}:
            continue
        inside = shapes.owner_key(key) is not None
        if not (inside and (key in reported or key in named)) and named.get(key) != "WV-TXN-HOLD-EXPIRED":
            continue
        error = _stopping_error(key, entry.kind, errors, uses, reported, None)
        if error is None and key in named:
            error = _named_error(key, entry.kind, named[key], sent="input" in entry.model_fields_set)
        yield index, {"status": "failed"} | ({"error": error} if error is not None else {})


def _named_error(key: str, kind: str, code: str, *, sent: bool) -> Any:
    """The error of a failure a transaction names by its code only; None for a code not spelled as one."""
    from firefly_weave.contracts.step_errors import HANDLED_KERNEL_CODES
    from firefly_weave.contracts.studio_documents import ERROR_CODE_PATTERN
    from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES

    if re.fullmatch(ERROR_CODE_PATTERN[1:-1], code) is None:
        return None
    # As the runtime counts them: an action or a call that was never sent made no attempt.
    tried = kind not in {"action", "callWorkflow"}
    started = sent or (tried and code not in HANDLED_KERNEL_CODES)
    attempts = 1 if sent or tried else 0
    return _error_object(
        key, code, "failed" if started else "not_started", attempts, message=TRANSACTION_MESSAGES.get(code)
    )


def _stopping_error(
    key: str,
    kind: str,
    errors: Mapping[str, Any],
    uses: Mapping[str, str],
    reported: Mapping[str, Mapping[str, Any]],
    incident: str | None,
) -> Any:
    """The error of the failure that stopped the run at the step instance ``key``: the simulated failure that applies
    to it (its instance key, then its step, then its action, as the simulator picks), else the failure it reported,
    else the incident the run is held on, in the incident's words."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.contracts.step_errors import HANDLED_KERNEL_CODES, RECONCILED_FAILURE_CODES

    step = node_of(key)
    for candidate in ("node:" + key, "node:" + step, "action:" + uses.get(step, "")):
        if (failure := errors.get(candidate)) is not None:
            return _error_object(key, failure.code, failure.outcome, failure.attempts)
    report = reported.get(key)
    if report is not None:
        outcome, attempts = report.get("outcome", "unknown"), report.get("attempts", 1)
        return _error_object(key, report.get("code"), outcome, attempts, report.get("child_run_id"))
    if incident is None:
        return None
    # A step that couldn't be evaluated did nothing; an effect that may have happened is unknown.
    if incident in HANDLED_KERNEL_CODES:
        outcome = "not_started"
    elif incident in RECONCILED_FAILURE_CODES:
        outcome = "unknown"
    else:
        outcome = "failed"
    # As the runtime counts them: an action or a call that never started made no attempt.
    attempts = 0 if outcome == "not_started" and kind in {"action", "callWorkflow"} else 1
    return _error_object(key, incident, outcome, attempts, message=_incident_words(incident))


def _frame_status(status: str) -> Literal["running", "waiting", "completed", "failed"]:
    """A called run's status in the run log's words: one that was cancelled or timed out failed where it stopped."""
    if status == "succeeded":
        return "completed"
    if status in {"failed", "cancelled", "timed_out"}:
        return "failed"
    return "waiting" if status in {"waiting", "suspended"} else "running"


def _incident(status: str, variables: Mapping[str, object]) -> str | None:
    """The code of the incident a run is held on, or None.

    The runtime opens one for a step it refuses outside a transaction (a loop over more items than its limit, say):
    it undoes that event's work and holds the run until a person resolves the incident.
    """
    code = variables.get("incident")
    return code if status == "suspended" and isinstance(code, str) else None


def _clocks(data: DebugState, terminal: set[str]) -> Iterator[tuple[RunState, list[Deadline]]]:
    """The runs whose deadlines move the clock: the simulated workflow's, and each called run that hasn't ended. Not a
    called run held on an incident, nor those waiting on it: they stay as they are until a person resolves it."""
    if data.state is not None:
        yield data.state, data.deadlines
    held = [frame.key for frame in data.frames if frame.state is not None and frame.state.status == "suspended"]
    for frame in data.frames:
        if frame.state is None or frame.state.status in terminal:
            continue
        if not any(key == frame.key or key.startswith(frame.key + "/") for key in held):
            yield frame.state, frame.deadlines


def _drive(simulator: Any, scripts: Any, signal_node: Any, human_node: Any, terminal: set[str]) -> Any:
    """Run until the end, the target, an incident (it waits for a person), or a signal or a person nobody scripted;
    waits advance by themselves, those of the called workflows too."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.operations.debug.transactions import due_deadline
    from firefly_weave.studio.local_contracts import Blocked

    nodes = {node.id: node for node in simulator.ir.graph.nodes}
    for _ in range(ROUNDS):
        view = simulator.continue_until_breakpoint()
        data = simulator.data
        if (
            view.diagnostics
            or view.status in terminal
            or data.paused is not None
            or _incident(view.status, view.variables) is not None
        ):
            return None
        moved = False
        for key in list(view.active_nodes):
            node = nodes.get(node_of(key))
            if isinstance(node, signal_node):
                script = scripts.signals.get(key) or scripts.signals.get(node.id)
                if script is None:
                    return Blocked(
                        reason="signal",
                        nodeId=node.id,
                        instanceKey="" if key == node.id else key,
                        message=f"Send signal {node.name}",
                    )
                entry = script.model_dump(mode="json", by_alias=True)
                if entry.get("outcome") == "timeout":
                    continue
                if entry.get("afterSeconds"):
                    simulator.advance_time(int(entry["afterSeconds"]))
                simulator.signal(node.name, entry.get("payload"))
                moved = True
            elif key in data.asks:
                # A task an AI agent gave a person: a call's review, or a tool's question. Only its own key answers it.
                script = scripts.humans.get(key)
                if script is None:
                    title = cast(dict[str, Any], data.inputs.get(key) or {}).get("title")
                    return Blocked(
                        reason="human",
                        nodeId=node_of(key),
                        instanceKey=key,
                        message=title if isinstance(title, str) and title else f"Decide for {key}",
                    )
                entry = script.model_dump(mode="json", by_alias=True)
                if entry.get("outcome") == "expire":
                    continue
                simulator.human_decision(key, {"decision": entry["decision"], "data": entry.get("data", {})})
                moved = True
            elif isinstance(node, human_node):
                script = scripts.humans.get(key) or scripts.humans.get(node.id)
                if script is None:
                    return Blocked(
                        reason="human",
                        nodeId=node.id,
                        instanceKey="" if key == node.id else key,
                        message=f"Decide for {node.id}",
                    )
                entry = script.model_dump(mode="json", by_alias=True)
                if entry.get("outcome") == "expire":
                    continue
                simulator.human_decision(key, {"decision": entry["decision"], "data": entry.get("data", {})})
                moved = True
            if moved:
                break
        if moved:
            continue
        # Only a deadline its run would still take: one left behind by a step that ended would hold the clock.
        due = [
            deadline.deadline
            for state, deadlines in _clocks(data, terminal)
            for deadline in deadlines
            if deadline.deadline >= data.now and due_deadline(state, deadline)
        ]
        if not due:
            return None
        simulator.advance_time(max(0, math.ceil((min(due) - data.now).total_seconds())))
    return None


def misfitting_mocks(document: JsonObject, mocks: Mapping[str, JsonValue], catalog: CatalogSnapshot) -> set[str]:
    """Step mocks that no longer fit their step's output fields, or those of the confirm, cancel or undo they stand
    for: left out, so the step asks for test data instead of the whole run being refused for a pin it may never
    reach."""
    from firefly_weave.compiler.platform_catalog import platform_schema_bundle
    from firefly_weave.compiler.schemas import validate_payload
    from firefly_weave.compiler.test_data import pin_target

    bundle = platform_schema_bundle() | {name: schema.value for name, schema in catalog.schemas.items()}
    misfits = set()
    for key, value in mocks.items():
        if not key.startswith("node:"):
            continue
        tools = _call_tools(document, key.removeprefix("node:"), catalog)
        if tools is not None:
            # A mock of one AI agent call is a tool's result, not the agent's output: it fits a tool's or misfits.
            if not _fits_a_tool(tools, value, bundle):
                misfits.add(key)
            continue
        target = pin_target(document, key.removeprefix("node:"), catalog)
        if isinstance(target, dict) and validate_payload(target, value, bundle):
            misfits.add(key)
    return misfits
