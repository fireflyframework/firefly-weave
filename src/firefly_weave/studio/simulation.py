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
import math
from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.values import JsonObject, JsonValue

if TYPE_CHECKING:
    from firefly_weave.studio.local_contracts import ExecuteRequest, ExecuteResponse, SimulationRequest

NULL: JsonObject = {"literal": None}


def _branches(step: JsonObject) -> list[JsonObject]:
    """The step lists a container holds, each with its own ``steps`` and ``output``."""
    kind = step.get("kind")
    if kind == "switch":
        return [*cast(list[JsonObject], step.get("cases") or []), cast(JsonObject, step.get("default") or {})]
    if kind == "parallel":
        return list(cast(dict[str, JsonObject], step.get("branches") or {}).values())
    if kind == "forEach":
        return [cast(JsonObject, step.get("body") or {})]
    if kind == "transaction":
        return [step]
    return []


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
        for other, sibling in enumerate(_branches(kept)):
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
    """What ships with Weave: the HTTP, email and file connectors and the AI action, so they compile offline."""
    from firefly_weave.connectors import email
    from firefly_weave.contracts import agentic
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
            load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value),
        ],
        tasks=[*(task for d in descriptors for task in d.capabilities), agentic.task_capability()],
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


def secret_paths(document: JsonObject, request: SimulationRequest, catalog: CatalogSnapshot) -> list[str]:
    """Pointers into the request of values a schema marks secret, or that can't be classified: never simulated."""
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.compiler.test_data import output_target, step_at
    from firefly_weave.operations.redaction import classify

    bundle = {name: schema.value for name, schema in catalog.schemas.items()}
    spec = document.get("spec") if isinstance(document.get("spec"), dict) else {}
    found: list[str] = []

    def check(path: str, schema: object, value: JsonValue) -> None:
        if not isinstance(schema, dict):
            return
        try:
            classify(schema, value, bundle)
        except (_Failure, RecursionError, ArithmeticError):
            found.append(path)

    check("/input", cast(JsonObject, spec).get("inputSchema"), request.input)
    for key, value in request.mocks.items():
        if key.startswith("node:"):
            step = step_at(document, key.removeprefix("node:"))
            check(_pointer("mocks", key), output_target(document, step, catalog), value)
    for key, signal in request.scripts.signals.items():
        step = step_at(document, key)
        payload = signal.model_dump(mode="json", by_alias=True).get("payload")
        check(_pointer("scripts", "signals", key, "payload"), (step or {}).get("payloadSchema"), payload)
    for key, answer in request.scripts.humans.items():
        step = step_at(document, key)
        data = answer.model_dump(mode="json", by_alias=True).get("data")
        check(_pointer("scripts", "humans", key, "data"), (step or {}).get("formSchema"), data)
    return found


def _label(switch: JsonObject, branch: str) -> str:
    """A decision's path in plain words: "path 2" for its second case, "Otherwise" for its default."""
    if branch.startswith("case:") and branch[5:].isdigit():
        return f"path {int(branch[5:]) + 1}"
    return "Otherwise"


def _enclosing(document: JsonObject, step_id: str) -> list[JsonObject]:
    """The decisions holding a step, outermost first."""
    found: list[JsonObject] = []

    def walk(steps: object, outer: list[JsonObject]) -> bool:
        if not isinstance(steps, list):
            return False
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("id") == step_id:
                found.extend(outer)
                return True
            chain = [*outer, step] if step.get("kind") == "switch" else outer
            if any(walk(branch.get("steps"), chain) for branch in _branches(step)):
                return True
        return False

    spec = document.get("spec")
    walk(spec.get("steps") if isinstance(spec, dict) else None, [])
    return found


def execute(request: ExecuteRequest, project: CatalogSnapshot | None) -> ExecuteResponse:
    """Simulate the target of an Execute request; the answer is ``ExecuteResponse``.

    Raises ``SimulationRefused`` for a request the host must refuse, and ``CatalogConflict`` for a local definition
    that conflicts with the catalog. Nothing here reads the network, the clock or files.
    """
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.ir import HumanTaskNode, SignalNode
    from firefly_weave.compiler.parser import ParseFailure, parse_source
    from firefly_weave.contracts.diagnostics import Diagnostic
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.operations.debug.ai_turns import with_ai_turns
    from firefly_weave.operations.debug.models import DebugError
    from firefly_weave.operations.debug.simulator import TERMINAL, Simulator, first_instance
    from firefly_weave.runtime.kernel import KernelError
    from firefly_weave.studio.local_contracts import (
        Blocked,
        CompileSummary,
        ExecuteResponse,
        LoopProgress,
        SelectedScope,
        TraceEntry,
    )

    if not request.scripts.autoAdvanceWaits:
        raise SimulationRefused(422, "WV-STUDIO-REQUEST", "Execute advances waits; step through a session instead")
    target = request.target
    try:
        document: JsonObject = parse_source(request.source, format=request.format).value
    except ParseFailure as failure:
        summary = CompileSummary(ok=False, sliced=False, diagnostics=list(failure.diagnostics), stubs=[])
        return ExecuteResponse(status="failed", compile=summary, trace=[])
    whole = document
    sliced = False
    if target.mode != "workflow":
        part = slice_for_target(document, cast(str, target.stepId))
        if part is None:
            raise SimulationRefused(422, "WV-STUDIO-REQUEST", "That step isn't in this workflow")
        document, sliced = part, True
    catalog, stubs = simulation_catalog(document, request.definitions, project, stub_unresolved=request.stubUnresolved)
    refused = secret_paths(whole, request, catalog)
    if refused:
        raise SimulationRefused(
            422, "WV-SCHEMA-SECRET_VALUE", "Test data can't hold values marked secret; leave them out", refused
        )
    result = compile_source(document, format="object", catalog=catalog)
    summary = CompileSummary(
        ok=result.ok,
        sliced=sliced,
        artifactDigest=result.artifact.digest if result.artifact is not None else None,
        diagnostics=list(result.diagnostics),
        stubs=stubs,
    )
    if not result.ok or result.artifact is None:
        return ExecuteResponse(status="failed", compile=summary.model_copy(update={"ok": False}), trace=[])
    artifact = result.artifact
    scripts = request.scripts
    misfits = misfitting_mocks(whole, request.mocks, catalog)
    ai_turns = {key: entry.model_dump(mode="json", by_alias=True) for key, entry in scripts.aiTurns.items()}
    try:
        mocks = with_ai_turns(artifact, {k: v for k, v in request.mocks.items() if k not in misfits}, ai_turns)
        try:
            simulator = Simulator(artifact, mocks=mocks, input=request.input, now=request.now)
        except KernelError as error:
            # The test input doesn't fit the workflow's input fields: said with its pointer, never its value.
            raise SimulationRefused(
                422, str(error), "The test input doesn't fit the input fields", ["/input"]
            ) from None
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
    except DebugError as error:
        if error.code == "WV-DEBUG-LIMIT":
            return ExecuteResponse(status="limit", compile=summary, trace=[])
        raise SimulationRefused(422, error.code, "The simulation couldn't use this test data") from None
    view = simulator.inspect()
    data = simulator.data
    steps = {step.get("id") for step in steps_of(cast(JsonObject, whole["spec"]).get("steps"))}
    trace: list[TraceEntry] = []
    seen: set[tuple[str | None, str]] = set()
    failed = view.status == "failed" and not view.diagnostics
    for boundary in data.boundaries:
        if boundary.kind != "node" or boundary.node_id is None or boundary.frame is not None:
            continue
        key = boundary.node_id
        if node_of(key) not in steps or (None, key) in seen:
            continue
        seen.add((None, key))
        reached = "completed" if key in data.outputs else "waiting" if key in view.active_nodes else "running"
        entry: dict[str, Any] = {
            "index": len(trace),
            "nodeId": node_of(key),
            "instanceKey": "" if key == node_of(key) else key,
            "kind": cast(str, boundary.node_kind),
            "status": reached,
        }
        if key in data.inputs:
            entry["input"] = data.inputs[key]
        if key in data.outputs:
            entry["output"] = data.outputs[key]
        trace.append(TraceEntry.model_validate(entry))
    if failed and trace and trace[-1].status != "completed":
        trace[-1] = trace[-1].model_copy(update={"status": "failed"})
    missing = next((d for d in view.diagnostics if d.code == "WV-DEBUG-MISSING_MOCK"), None)
    if blocked is None and missing is not None:
        last = next(reversed(data.inputs), None) if data.inputs else None
        if last is not None and last not in data.outputs:
            step = node_of(last)
            blocked = Blocked(
                reason="missing_mock",
                nodeId=step,
                instanceKey="" if last == step else last,
                message=(
                    f"The pinned output of {step} doesn't fit its output fields. Edit it, or unpin it."
                    if {"node:" + last, "node:" + step} & misfits
                    else f"Step {step} needs test data."
                ),
            )
    if blocked is None and target.mode != "workflow" and view.status not in {"paused"} and not view.diagnostics:
        step = cast(str, target.stepId)
        if not any(entry.nodeId == step for entry in trace):
            for switch in reversed(_enclosing(whole, step)):
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
    elif view.diagnostics or view.status == "failed":
        status = "failed"
    else:
        status = "completed"
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
        variables=view.variables,
        diagnostics=[d for d in view.diagnostics if d.code != "WV-DEBUG-MISSING_MOCK" or blocked is None],
    )


def _drive(simulator: Any, scripts: Any, signal_node: Any, human_node: Any, terminal: set[str]) -> Any:
    """Run until the end, the target, or a signal or a person nobody scripted; waits advance by themselves."""
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.studio.local_contracts import Blocked

    nodes = {node.id: node for node in simulator.ir.graph.nodes}
    for _ in range(ROUNDS):
        view = simulator.continue_until_breakpoint()
        data = simulator.data
        if view.diagnostics or view.status in terminal or data.paused is not None:
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
        due = [d.deadline for d in data.deadlines if d.deadline >= data.now]
        if not due:
            return None
        simulator.advance_time(max(0, math.ceil((min(due) - data.now).total_seconds())))
    return None


def misfitting_mocks(document: JsonObject, mocks: Mapping[str, JsonValue], catalog: CatalogSnapshot) -> set[str]:
    """Step mocks that no longer fit their step's output fields: left out, so the step asks for test data instead of
    the whole run being refused for a pin it may never reach."""
    from firefly_weave.compiler.schemas import validate_payload
    from firefly_weave.compiler.test_data import output_target, step_at

    bundle = {name: schema.value for name, schema in catalog.schemas.items()}
    misfits = set()
    for key, value in mocks.items():
        if not key.startswith("node:"):
            continue
        target = output_target(document, step_at(document, key.removeprefix("node:")), catalog)
        if isinstance(target, dict) and validate_payload(target, value, bundle):
            misfits.add(key)
    return misfits
