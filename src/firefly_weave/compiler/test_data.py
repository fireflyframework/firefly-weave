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

"""Check every entry of a workflow's test data against the schema it belongs to.

Test data follows the platform's rule for values: a present value at a location a schema marks ``x-secret`` or
``writeOnly`` is refused, and so is a value that can't be classified. Each entry is checked against its own schema:
the workflow input against ``spec.inputSchema``, a pinned output against the step's output schema (an action's, an AI
task profile's, a called workflow's), a signal payload against ``payloadSchema``, a person's answer against the task's
``formSchema``, and a called workflow's mocks against the steps of that version. The Studio host answers with every
entry's result; the platform's studio documents refuse secrets and entries it can't classify.
"""

from collections.abc import Iterator
from typing import Any, Literal, cast

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.llm import llm_output_schema
from firefly_weave.compiler.schemas import _Failure, validate_payload
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of
from firefly_weave.contracts.studio_documents import StudioTestData
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.redaction import classify

type CheckCode = Literal[
    "WV-SCHEMA-SECRET_VALUE", "WV-SCHEMA-CLASSIFICATION", "WV-STUDIO-TESTDATA-SCHEMA", "WV-STUDIO-TESTDATA-UNRESOLVED"
]
SECRET: CheckCode = "WV-SCHEMA-SECRET_VALUE"
UNCLASSIFIED: CheckCode = "WV-SCHEMA-CLASSIFICATION"
SCHEMA: CheckCode = "WV-STUDIO-TESTDATA-SCHEMA"
UNRESOLVED: CheckCode = "WV-STUDIO-TESTDATA-UNRESOLVED"
# A schema the entry is checked against, or the code that says why there is none.
type Target = JsonObject | CheckCode

_NOTHING: JsonObject = {"type": "null"}
# The phases a step inside a transaction may be undone or confirmed with (`hold#confirm`, `add#undo`). Each runs
# another action, so its output is never checked against the schema of the step it belongs to.
_PHASES = frozenset({"confirm", "cancel", "undo"})


def _phase(key: str) -> bool:
    """Whether a key names an undo or confirm phase: its activation part ends with a phase name."""
    activation = key.partition("#")[2].partition("~")[0]
    return bool(activation) and activation.rsplit(".", 1)[-1] in _PHASES


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _steps(steps: object) -> Iterator[JsonObject]:
    """Every step in a step list, nested ones included (the containers the language declares)."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        kind = step.get("kind")
        branches: list[object] = []
        if kind == "switch":
            branches = [*cast(list[object], step.get("cases") or []), step.get("default")]
        elif kind == "parallel":
            branches = list(cast(dict[str, object], step.get("branches") or {}).values())
        elif kind == "forEach":
            branches = [step.get("body")]
        for branch in branches:
            if isinstance(branch, dict):
                yield from _steps(branch.get("steps"))


def step_at(workflow: JsonObject, key: str) -> JsonObject | None:
    """The step an instance key (`check`, `send[2]`) names, anywhere in the workflow, or None."""
    try:
        identifier = node_of(key)
    except InvalidInstanceKey:
        return None
    spec = workflow.get("spec")
    steps = spec.get("steps") if isinstance(spec, dict) else None
    return next((step for step in _steps(steps) if step.get("id") == identifier), None)


def _document(catalog: CatalogSnapshot, kind: Literal["Action", "Workflow"], reference: object) -> JsonObject | None:
    resource = catalog.resources.get((kind, reference)) if isinstance(reference, str) else None
    return resource.definition.value if resource is not None else None


def _spec(document: JsonObject | None) -> JsonObject:
    spec = document.get("spec") if document is not None else None
    return spec if isinstance(spec, dict) else {}


def output_target(workflow: JsonObject, step: JsonObject | None, catalog: CatalogSnapshot) -> Target:
    """The schema a pinned output or a mock of this step is checked against."""
    if step is None:
        return UNRESOLVED
    kind = step.get("kind")
    if kind == "action":
        document = _document(catalog, "Action", step.get("uses"))
        schema = _spec(document).get("outputSchema")
        return schema if isinstance(schema, dict) else UNRESOLVED
    if kind == "llm":
        profiles = _spec(workflow).get("llmProfiles")
        name = step.get("profile")
        profile = profiles.get(name) if isinstance(profiles, dict) and isinstance(name, str) else None
        try:
            return llm_output_schema(cast(JsonObject, profile))
        except (KeyError, TypeError, AttributeError):
            return UNRESOLVED
    if kind == "callWorkflow":
        if step.get("mode") == "detach":
            return _NOTHING
        schema = _spec(_document(catalog, "Workflow", step.get("uses"))).get("outputSchema")
        return schema if isinstance(schema, dict) else UNRESOLVED
    # An AI agent's output schema comes from its own step settings, published with that step kind.
    if kind == "agent":
        return UNRESOLVED
    # Other kinds compute their output from their input: nothing to pin.
    return SCHEMA


# The module's own callers keep the short names.
_step = step_at
_output = output_target


def _framed(workflow: JsonObject, frame: object, catalog: CatalogSnapshot) -> JsonObject | None:
    """The called workflow a frame (`enrich`, `notify[2]/enrich`) runs in, or None when it isn't known here."""
    if not isinstance(frame, str) or not frame:
        return workflow
    current: JsonObject | None = workflow
    for part in frame.split("/"):
        call = _step(current, part) if current is not None else None
        if call is None or call.get("kind") != "callWorkflow":
            return None
        current = _document(catalog, "Workflow", call.get("uses"))
    return current


def _verdict(target: Target, value: JsonValue, bundle: dict[str, JsonObject]) -> CheckCode | None:
    if isinstance(target, str):
        return target
    try:
        classify(target, value, bundle)
    except _Failure as failure:
        return SECRET if failure.code == "SECRET_VALUE" else UNCLASSIFIED
    except (RecursionError, ArithmeticError):
        return UNCLASSIFIED
    return SCHEMA if validate_payload(target, value, bundle) else None


def _targets(
    workflow: JsonObject, data: StudioTestData, catalog: CatalogSnapshot
) -> Iterator[tuple[str, Target, JsonValue]]:
    document = data.model_dump(mode="json", by_alias=True)
    spec = _spec(workflow)
    if "input" in document:
        schema = spec.get("inputSchema")
        yield "/input", schema if isinstance(schema, dict) else UNRESOLVED, document["input"]["value"]
    for key, pin in cast(dict[str, Any], document.get("pins", {})).items():
        output: Target = UNRESOLVED if _phase(key) else _output(workflow, _step(workflow, key), catalog)
        yield f"/pins/{_escape(key)}", output, pin["output"]
    for key, signal in cast(dict[str, Any], document.get("signals", {})).items():
        if "outcome" in signal:
            yield f"/signals/{_escape(key)}", _NOTHING, None
            continue
        frame = _framed(workflow, signal.get("frame"), catalog)
        step = _step(frame, key) if frame is not None else None
        schema = step.get("payloadSchema") if step is not None and step.get("kind") == "signal" else None
        yield f"/signals/{_escape(key)}", schema if isinstance(schema, dict) else UNRESOLVED, signal["payload"]
    for key, answer in cast(dict[str, Any], document.get("humans", {})).items():
        if "outcome" in answer:
            yield f"/humans/{_escape(key)}", _NOTHING, None
            continue
        frame = _framed(workflow, answer.get("frame"), catalog)
        step = _step(frame, key) if frame is not None else None
        if step is None or step.get("kind") != "humanTask":
            yield f"/humans/{_escape(key)}", UNRESOLVED, answer["data"]
            continue
        decisions = step.get("decisions", ["approve", "reject"])
        schema = step.get("formSchema")
        target: Target = (
            SCHEMA
            if not isinstance(decisions, list) or answer["decision"] not in decisions
            else schema
            if isinstance(schema, dict)
            else UNRESOLVED
        )
        yield f"/humans/{_escape(key)}", target, answer["data"]
    # Recorded AI turns are checked against the agent's turn schema, which comes with the AI steps.
    for key, turns in cast(dict[str, Any], document.get("aiTurns", {})).items():
        yield f"/aiTurns/{_escape(key)}", UNRESOLVED, turns
    for reference, callee in cast(dict[str, Any], document.get("workflows", {})).items():
        called = _document(catalog, "Workflow", reference)
        for key, mock in cast(dict[str, Any], callee["mocks"]).items():
            step = _step(called, key) if called is not None and not _phase(key) else None
            path = f"/workflows/{_escape(reference)}/mocks/{_escape(key)}"
            yield path, _output(called or {}, step, catalog), mock["output"]


def check_test_data(
    workflow: JsonObject, data: StudioTestData, catalog: CatalogSnapshot
) -> list[tuple[str, CheckCode | None]]:
    """Each entry's pointer and why it is refused, or None when it may be kept; in document order."""
    bundle = {name: schema.value for name, schema in catalog.schemas.items()}
    return [(path, _verdict(target, value, bundle)) for path, target, value in _targets(workflow, data, catalog)]
