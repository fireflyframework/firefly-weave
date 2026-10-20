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


"""Workflow calls: which workflow versions a workflow may call, how deep calls go, and what a call step returns.

A ``callWorkflow`` step names an exact published workflow version. The called version must opt in with
``spec.callable`` (optionally limited to named callers), must not lead back to the calling workflow, and the
longest chain of calls it starts must stay within ``Limits.max_call_depth``. ``check_call_target`` applies these
checks to one call; anything else that starts workflows (such as an AI agent's workflow tool) applies the same
function.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot
from firefly_weave.compiler.ir import allows_caller
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.contracts.values import JsonObject, JsonValue

# How a waiting call that continues on failure reports the child run's outcome.
CALL_STATUSES: Final = ("succeeded", "failed", "cancelled", "timed_out")
BUSINESS_KEY_SCHEMA: Final[JsonObject] = {"type": "string", "minLength": 1, "maxLength": 200}
CALL_ERROR_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
    "required": ["code", "message"],
    "additionalProperties": False,
}
CALL_MESSAGES: Final[dict[str, str]] = {
    "UNKNOWN_WORKFLOW": "The catalog does not contain this exact workflow version.",
    "CALL_NOT_CALLABLE": (
        "This workflow version does not allow calls from {caller}; "
        "add {caller} to its callable.allowedCallers in a new version."
    ),
    "CALL_RECURSION": "This call would reach the calling workflow again.",
    "CALL_DEPTH": "The chain of workflow calls is deeper than {limit:,}.",
    "CALL_MODE": "onFailure applies only when the call waits for its result.",
}


@dataclass(frozen=True)
class CallIssue:
    """One finding about a call target; ``code`` has no ``WV-COMP-`` prefix."""

    code: str
    message: str
    stage: Literal["resolution", "semantic"]


@dataclass(frozen=True)
class CallTarget:
    """The resolved called workflow (``None`` when the catalog lacks it) and every finding about the call."""

    resource: CatalogResource | None
    issues: tuple[CallIssue, ...]


@dataclass
class CallGraph:
    """The calls each catalog workflow makes, read once per analysis."""

    catalog: CatalogSnapshot
    _calls: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Longest chains already computed; every workflow on a finished walk is final, so later calls reuse them.
    _depths: dict[str, int] = field(default_factory=dict)

    def calls(self, reference: str) -> tuple[str, ...]:
        """The exact references ``reference`` calls; none when the catalog lacks it."""
        if reference not in self._calls:
            resource = self.catalog.resolve("Workflow", reference)
            self._calls[reference] = tuple(called_references(resource.definition.value)) if resource else ()
        return self._calls[reference]

    def reaches(self, start: str, target: str) -> bool:
        """Whether ``start`` is ``target`` or calls it through any chain of calls."""
        seen, pending = {start}, deque([start])
        while pending:
            reference = pending.popleft()
            if reference == target:
                return True
            for callee in self.calls(reference):
                if callee not in seen:
                    seen.add(callee)
                    pending.append(callee)
        return False

    def depth(self, start: str) -> int | None:
        """The longest chain of calls ``start`` makes (0 when it calls nothing); ``None`` when calls form a cycle."""
        if start in self._depths:
            return self._depths[start]
        state: dict[str, bool] = {reference: True for reference in self._depths}
        state[start] = False
        order: list[str] = []
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(self.calls(start)))]
        while stack:
            reference, callees = stack[-1]
            callee = next(callees, None)
            if callee is None:
                stack.pop()
                state[reference] = True
                order.append(reference)
            elif callee not in state:
                state[callee] = False
                stack.append((callee, iter(self.calls(callee))))
            elif not state[callee]:
                return None
        depths = self._depths
        for reference in order:
            depths[reference] = max((depths[callee] + 1 for callee in self.calls(reference)), default=0)
        return depths[start]


def workflow_steps(document: JsonValue) -> Iterator[tuple[JsonObject, str]]:
    """Every step of a workflow definition with its path, blocks included; never recursive, never validating."""
    spec = document.get("spec") if isinstance(document, dict) else None
    if not isinstance(spec, dict):
        return
    blocks: deque[tuple[JsonValue, str]] = deque([(spec.get("steps"), "/spec/steps")])
    while blocks:
        steps, path = blocks.popleft()
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            if not isinstance(step, dict):
                continue
            location = f"{path}/{index}"
            yield step, location
            kind = step.get("kind")
            if kind == "switch":
                cases = step.get("cases")
                for number, case in enumerate(cases if isinstance(cases, list) else []):
                    if isinstance(case, dict):
                        blocks.append((case.get("steps"), f"{location}/cases/{number}/steps"))
                if isinstance(step.get("default"), dict):
                    blocks.append((cast(JsonObject, step["default"]).get("steps"), location + "/default/steps"))
            elif kind == "parallel" and isinstance(step.get("branches"), dict):
                for name, branch in sorted(cast(JsonObject, step["branches"]).items()):
                    if isinstance(branch, dict):
                        blocks.append((branch.get("steps"), pointer_child(location + "/branches", name) + "/steps"))
            elif kind == "forEach" and isinstance(step.get("body"), dict):
                blocks.append((cast(JsonObject, step["body"]).get("steps"), location + "/body/steps"))


def called_references(document: JsonValue) -> list[str]:
    """The exact workflow references a workflow definition calls, in document order."""
    return [
        cast(str, step["uses"])
        for step, _ in workflow_steps(document)
        if step.get("kind") == "callWorkflow" and isinstance(step.get("uses"), str)
    ]


def detached_failure_paths(document: JsonValue) -> list[str]:
    """Paths of ``onFailure`` on calls that do not wait for their result, which the definition model rejects."""
    return [
        path + "/onFailure"
        for step, path in workflow_steps(document)
        if step.get("kind") == "callWorkflow" and step.get("mode") == "detach" and "onFailure" in step
    ]


def without_detached_failures(document: JsonObject) -> JsonObject:
    """A copy of a workflow definition without ``onFailure`` on calls that do not wait for their result."""
    copy = deepcopy(document)
    for step, _ in workflow_steps(copy):
        if step.get("kind") == "callWorkflow" and step.get("mode") == "detach":
            step.pop("onFailure", None)
    return copy


def check_call_target(
    caller: str,
    reference: str,
    catalog: CatalogSnapshot,
    *,
    max_call_depth: int = MAX_CALL_DEPTH,
    graph: CallGraph | None = None,
) -> CallTarget:
    """Resolve the workflow ``caller`` (``name@version``) calls at ``reference`` and check that the call is allowed.

    Findings, in this order: the catalog lacks the exact version (``UNKNOWN_WORKFLOW``, nothing else is checked);
    the version does not allow calls from the caller's name (``CALL_NOT_CALLABLE``); the call leads back to the
    caller's exact version (``CALL_RECURSION``); or the chain of calls this call starts is longer than
    ``max_call_depth`` or never ends (``CALL_DEPTH``).
    """
    resource = catalog.resolve("Workflow", reference)
    if resource is None:
        return CallTarget(None, (CallIssue("UNKNOWN_WORKFLOW", CALL_MESSAGES["UNKNOWN_WORKFLOW"], "resolution"),))
    graph = graph or CallGraph(catalog)
    name = caller.partition("@")[0]
    issues: list[CallIssue] = []
    spec = resource.definition.value["spec"]
    if not allows_caller(cast(JsonObject, spec).get("callable"), name):
        message = CALL_MESSAGES["CALL_NOT_CALLABLE"].format(caller=name)
        issues.append(CallIssue("CALL_NOT_CALLABLE", message, "resolution"))
    if graph.reaches(reference, caller):
        issues.append(CallIssue("CALL_RECURSION", CALL_MESSAGES["CALL_RECURSION"], "semantic"))
    else:
        depth = graph.depth(reference)
        if depth is None or depth + 1 > max_call_depth:
            message = CALL_MESSAGES["CALL_DEPTH"].format(limit=max_call_depth)
            issues.append(CallIssue("CALL_DEPTH", message, "semantic"))
    return CallTarget(resource, tuple(issues))


def continue_envelope(output: JsonObject) -> JsonObject:
    """The output of a waiting call that continues when the child run does not succeed."""
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(CALL_STATUSES)},
            "runId": {"type": "string", "format": "uuid"},
            "output": output,
            "error": CALL_ERROR_SCHEMA,
        },
        "required": ["status", "runId"],
        "additionalProperties": False,
    }


def call_output_schema(mode: str, on_failure: str | None, callee_output: JsonObject) -> JsonObject:
    """A call step's output: the called workflow's output, the continue envelope, or null for a detached call."""
    if mode == "detach":
        return {"type": "null"}
    if on_failure == "continue":
        return continue_envelope(callee_output)
    return callee_output
