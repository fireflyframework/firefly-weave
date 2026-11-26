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
"""What an AI step's texts read in the saved draft, checked like a compile.

A step test runs a copy of the step alone, whose texts arrive as its input, so only the draft says what they read. The
part of the draft up to the step is compiled without the steps that nothing in it reads, and the step runs only when
that compile could check everything its texts read and found nothing wrong with it. Pure.
"""

from collections import defaultdict, deque
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Final, cast

from firefly_weave.compiler.agents import MESSAGES, agent_expressions, expression_references
from firefly_weave.compiler.analyzer import AnalysisResult, AnalyzedStep
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import ExpressionFailure, pointer_segments
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.step_tests import DraftStep
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests.synthesis import API_VERSION
from firefly_weave.studio.simulation import slice_for_target

SECRET_INPUT: Final = "WV-COMP-AI_SECRET_INPUT"
ABOUT_STEPS: Final = ("/spec/steps", "/spec/connections")
UNCHECKED: Final = (
    "What this step's texts read can't be checked as saved; finish the texts, the steps they read and the blocks "
    "around the step, publish the actions those steps use, then try again"
)
# A refusal names at most this many findings.
REPORTED: Final = 20
# Steps whose output their contract declares, with the guard the compile keeps once it knows that contract.
DECLARED: Final[Mapping[str, str]] = {
    "action": "action_output",
    "agent": "agent_output",
    "callWorkflow": "call_output",
    "decisionTable": "decision_output",
    "humanTask": "human_output",
    "llm": "action_output",
    "signal": "signal_payload",
}
# Every other step's output is computed: from these expressions, and from what each of its blocks gives.
COMPUTED: Final[Mapping[str, tuple[str, ...]]] = {
    "fail": (),
    "forEach": ("items",),
    "parallel": (),
    "switch": (),
    "transaction": (),
    "transform": ("value",),
    "wait": (),
}
# The fields that hold an expression, in a step or in a part of it, whatever the step's kind.
EXPRESSIONS: Final = (
    "businessKey",
    "context",
    "fixed",
    "instructions",
    "items",
    "output",
    "prompt",
    "title",
    "value",
    "when",
    "with",
)
PARTS: Final = ("body", "cancel", "compensate", "confirm", "default", "errorPath")
LISTS_OF_PARTS: Final = ("branches", "cases", "tools")


def draft_refusal(draft: DraftStep, metadata: JsonObject, catalog: CatalogSnapshot) -> CatalogError | None:
    """Why an AI step of the draft can't run on its own, or None; ``metadata`` is the step test's own version."""
    cut = _up_to(draft, metadata)
    if cut is None:
        return _refusal(UNCHECKED, [])
    part, emptied = cut
    compiled = compile_source(part, format="object", catalog=catalog)
    errors = [diagnostic for diagnostic in compiled.diagnostics if diagnostic.severity == "error"]
    secret = [error for error in errors if error.code == SECRET_INPUT]
    if secret:
        return _refusal(MESSAGES["AI_SECRET_INPUT"], secret)
    # A finding outside the steps and their connections is about the whole draft, so it's about this step too.
    whole = [error for error in errors if not any(_within(error.path, place) for place in ABOUT_STEPS)]
    # A compile counts an error before it leaves one out, so without any it left out only warnings and notes.
    unlisted = compiled.schema_truncated or (compiled.omitted_count and compiled.error_count)
    if compiled.analysis is None or unlisted or whole or _reads_emptied(cast(JsonObject, part["spec"]), emptied):
        return _refusal(UNCHECKED, errors)
    problems = _Checked(compiled.analysis, errors, emptied).problems(cast(str, draft.step["id"]))
    return None if problems is None else _refusal(UNCHECKED, problems or errors)


def _refusal(message: str, findings: Sequence[Diagnostic]) -> CatalogError:
    """Paths in the draft and codes, never values."""
    return CatalogError(
        422,
        "WV-STEPTEST-DRAFT",
        message,
        result={"diagnostics": [{"path": item.path, "code": item.code} for item in findings[:REPORTED]]},
    )


def _up_to(draft: DraftStep, metadata: JsonObject) -> tuple[JsonObject, frozenset[str]] | None:
    """The draft up to the step as a workflow of its own, or None when the step can't be cut out of it.

    Steps keep their places, so a finding's path is its path in the draft. What isn't before the step or around it is
    left out: the steps after it, its own error path, profiles and connections only other steps name, how the workflow
    starts, and what it returns. The steps before it that nothing here reads are emptied; their IDs come too.
    """
    step_id = cast(str, draft.step["id"])
    cut = slice_for_target(draft.workflow, step_id)
    if cut is None:
        return None
    for step, _ in workflow_steps(cut):
        if step.get("id") == step_id:
            step.pop("onError", None)
            step.pop("errorPath", None)
    emptied = _empty_unread(cut, step_id)
    whole = cast(JsonObject, cut["spec"])
    kept = ("inputSchema", "outputSchema", "steps", "output")
    spec: JsonObject = {name: whole[name] for name in kept if name in whole}
    for field, reference in (("connections", "connection"), ("llmProfiles", "profile")):
        declared = whole.get(field)
        if isinstance(declared, dict):
            names = _names(whole["steps"], reference)
            spec[field] = {name: value for name, value in declared.items() if name in names}
    return {"apiVersion": API_VERSION, "kind": "Workflow", "metadata": metadata, "spec": spec}, emptied


def _empty_unread(cut: JsonObject, step_id: str) -> frozenset[str]:
    """Put a step that does nothing in the place of each step that nothing which stays reads; the IDs of those gone.

    A failure stays, so the block it ends still never completes. So does a step without an ID, which no step can stand
    in for.
    """
    steps = {place: step for step, place in workflow_steps(cut)}
    stays = _stays(steps, step_id)
    emptied: set[str] = set()
    for place, step in steps.items():
        name = step.get("id")
        if place not in stays and isinstance(name, str) and step.get("kind") != "fail":
            emptied.add(name)
            step.clear()
            step.update({"id": name, "kind": "transform", "value": {"literal": None}})
    return frozenset(emptied)


def _stays(steps: Mapping[str, JsonObject], step_id: str) -> Collection[str]:
    """The places of the tested step, of the steps its texts read, directly or through others, and of their blocks.

    A step that stays keeps every step that any of its expressions reads, and each step around it, so a loop's item
    keeps the loop's items. It then compiles as it does in the whole draft. Where what an expression reads can't be
    told, every step stays.
    """
    named: defaultdict[JsonValue, list[str]] = defaultdict(list)
    for place, step in steps.items():
        if isinstance(step.get("id"), str):
            named[step["id"]].append(place)
    stays: set[str] = set()
    pending = list(named[step_id])
    while pending:
        place = pending.pop()
        if place in stays:
            continue
        stays.add(place)
        pending.extend(_holders(steps, place))
        for expression in _expressions(steps[place]):
            for pointer in expression_references(expression):
                try:
                    segments = pointer_segments(pointer)
                except ExpressionFailure:
                    return steps.keys()
                if segments in ((), ("steps",)):
                    return steps.keys()
                if segments[0] == "steps":
                    pending.extend(named[segments[1]])
    return stays


def _holders(steps: Mapping[str, JsonObject], place: str) -> Iterator[str]:
    """The places of the steps around a place, the innermost first."""
    while place := place.rpartition("/")[0]:
        if place in steps:
            yield place


def _expressions(step: JsonObject) -> Iterator[JsonValue]:
    """Every expression a step as saved holds, in it and in its blocks, undos, tools and memory; never validating."""
    parts: list[JsonValue] = [step, *(step.get(name) for name in PARTS)]
    for name in LISTS_OF_PARTS:
        several = step.get(name)
        parts.extend(several.values() if isinstance(several, dict) else several if isinstance(several, list) else ())
    for part in parts:
        if isinstance(part, dict):
            yield from (part[name] for name in EXPRESSIONS if name in part)
    memory = step.get("memory")
    if isinstance(memory, dict) and isinstance(memory.get("key"), dict):
        yield memory["key"]


def _names(steps: JsonValue, field: str) -> set[str]:
    """Every name the field holds anywhere in the steps: a step's, a tool's, or an undo's."""
    return {name for node in _objects(steps) if isinstance(name := node.get(field), str)}


def _reads_emptied(spec: JsonObject, emptied: Collection[str]) -> bool:
    """Whether a step that stays reads one that was emptied: a check of ``_stays`` that knows no field names.

    Every ``{"ref": pointer}`` in the steps counts, wherever it is. A pointer that reads every step reads the emptied
    ones too; one that can't be decoded reads none, and the compile finds it. A step left in place of one reads nothing.
    """
    if not emptied:
        return False
    for node in _objects(spec["steps"]):
        pointer = node.get("ref")
        if not isinstance(pointer, str):
            continue
        try:
            segments = pointer_segments(pointer)
        except ExpressionFailure:
            continue
        if segments in ((), ("steps",)) or (segments[0] == "steps" and segments[1] in emptied):
            return True
    return False


def _objects(tree: JsonValue) -> Iterator[JsonObject]:
    """Every object anywhere in the tree."""
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            yield node
            pending.extend(node.values())


class _Checked:
    """What a compile of the draft up to the step settled."""

    def __init__(self, analysis: AnalysisResult, errors: Sequence[Diagnostic], emptied: Collection[str]) -> None:
        self.steps = {step.id: step for step in _every(analysis.typed_graph)}
        self.guards = {(guard.path, guard.purpose) for guard in analysis.runtime_guards}
        self.errors = errors
        self.emptied = emptied

    def errors_in(self, *places: str) -> list[Diagnostic]:
        return [error for error in self.errors if any(_within(error.path, place) for place in places)]

    def settled(self, step: AnalyzedStep) -> bool:
        """Whether the compile resolved the contracts the step's output comes from."""
        if step.kind in DECLARED:
            # A call that doesn't wait gives only the platform's own identity of the run it started.
            detached = step.kind == "callWorkflow" and step.definition.value.get("mode") == "detach"
            if not detached and (step.path, DECLARED[step.kind]) not in self.guards:
                return False
        elif step.kind not in COMPUTED:
            return False
        if step.kind == "forEach" and not all(branch.completes for branch in step.branches):
            return False
        return not self.errors_in(*(path for _, path in _output(step)))

    def problems(self, step_id: str) -> list[Diagnostic] | None:
        """None when the compile settled everything the step's texts read, directly or through other steps.

        Otherwise the findings there, maybe none: a contract is reported once, at the first step that names it. This
        follows the compiled steps, where ``_stays`` followed them as saved: a step it finds emptied was read after all.
        """
        target = self.steps.get(step_id)
        if target is None or not self.settled(target):
            return list(self.errors)
        texts = _texts(target)
        found = self.errors_in(*(path for _, path in texts))
        settled = not found
        seen = {target.id}
        reading = deque((target, expression) for expression, _ in texts)
        while reading:
            reader, expression = reading.popleft()
            for name in self.reads(reader, expression):
                if name in seen:
                    continue
                seen.add(name)
                step = self.steps.get(name)
                if step is None or name in self.emptied or not self.settled(step):
                    settled = False
                    found.extend(self.errors_in(step.path) if step is not None else [])
                if step is not None:
                    reading.extend((step, part) for part, _ in _output(step))
        return None if settled else found

    def reads(self, reader: AnalyzedStep, expression: JsonObject) -> Iterator[str]:
        """The steps an expression reads; a loop's item is read from the loop."""
        for pointer in expression_references(expression):
            try:
                segments = pointer_segments(pointer)
            except ExpressionFailure:
                segments = ()
            root = segments[0] if segments else ""
            if root in {"", "item", "loops"}:
                # Scopes alternate a step's ID and the name of its block.
                yield from (name for name in reader.scope[::2] if self.steps[name].kind == "forEach")
            if root == "steps" and len(segments) > 1:
                yield segments[1]
            elif root in {"", "steps"}:
                yield from self.steps


def _every(steps: Sequence[AnalyzedStep]) -> Iterator[AnalyzedStep]:
    for step in steps:
        yield step
        for branch in step.branches:
            yield from _every(branch.steps)


def _within(path: str, place: str) -> bool:
    return path == place or path.startswith(place + "/")


def _texts(step: AnalyzedStep) -> list[tuple[JsonObject, str]]:
    """What the model reads of the step: each expression with its path.

    A step test gives an agent no memory but its own step's, so nothing another agent keeps reaches its model.
    """
    value = step.definition.value
    if step.kind == "agent":
        memory = step.path + "/memory"
        return [item for item in agent_expressions(value, step.path) if not _within(item[1], memory)]
    return [(cast(JsonObject, value[name]), f"{step.path}/{name}") for name in ("prompt", "context")]


def _output(step: AnalyzedStep) -> list[tuple[JsonObject, str]]:
    """The expressions a step's output is computed from, with their paths: its own, and each of its blocks'."""
    value = step.definition.value
    own = [(cast(JsonObject, value[name]), f"{step.path}/{name}") for name in COMPUTED.get(step.kind, ())]
    return own + [(branch.output.value, branch.path + "/output") for branch in step.branches]
