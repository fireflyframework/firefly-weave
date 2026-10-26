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

import copy
import functools
from collections.abc import Iterable, Iterator
from typing import Any, cast

from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.values import JsonObject, JsonValue

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
