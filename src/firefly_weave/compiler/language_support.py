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

"""Find language constructs whose feature this compiler does not compile yet.

The definition schema accepts every frozen construct (language milestone M0); the compiler reports each use of
a feature outside ``COMPILED_FEATURES`` as ``WV-COMP-UNSUPPORTED_FEATURE`` instead of lowering it. Each language
milestone that lands a feature's compiler support adds that feature here.
"""

from collections import deque
from dataclasses import dataclass
from typing import Final, cast

from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.language_features import KIND_FEATURES, OPERATOR_FEATURES, LanguageFeature
from firefly_weave.contracts.values import JsonObject, JsonValue

COMPILED_FEATURES: Final[frozenset[LanguageFeature]] = frozenset()

# Expression positions per step kind; blocks are walked separately.
_EXPRESSIONS: Final[dict[str, tuple[str, ...]]] = {
    "action": ("with",),
    "llm": ("prompt", "context"),
    "transform": ("value",),
    "decisionTable": ("with",),
    "humanTask": ("title", "context"),
    "forEach": ("items",),
    "callWorkflow": ("with", "businessKey"),
}


@dataclass(frozen=True)
class UnsupportedUse:
    path: str
    construct: str
    feature: LanguageFeature

    @property
    def message(self) -> str:
        return (
            f"{self.construct} needs the language feature {self.feature}, "
            "which this version of Firefly Weave does not compile yet."
        )


def unsupported_uses(document: JsonObject) -> list[UnsupportedUse]:
    """Every use of a kind or operator whose feature is not compiled, sorted by path; iterative, never recursive."""
    found: list[UnsupportedUse] = []
    expressions: list[tuple[JsonValue, str]] = []
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return found
    if document.get("kind") == "Workflow":
        blocks: deque[tuple[JsonValue, str]] = deque([(spec.get("steps"), "/spec/steps")])
        expressions.append((spec.get("output"), "/spec/output"))
        while blocks:
            steps, path = blocks.popleft()
            for index, step in enumerate(steps if isinstance(steps, list) else []):
                if not isinstance(step, dict):
                    continue
                location = f"{path}/{index}"
                kind = step.get("kind")
                feature = KIND_FEATURES.get(kind) if isinstance(kind, str) else None
                if feature is not None and feature not in COMPILED_FEATURES:
                    found.append(UnsupportedUse(location + "/kind", f"The {kind} step", feature))
                for name in _EXPRESSIONS.get(str(kind), ()):
                    if name in step:
                        expressions.append((step[name], f"{location}/{name}"))
                for branch, branch_path in _branches(step, location):
                    if "when" in branch:
                        expressions.append((branch["when"], branch_path + "/when"))
                    blocks.append((branch.get("steps"), branch_path + "/steps"))
                    expressions.append((branch.get("output"), branch_path + "/output"))
    elif document.get("kind") == "DecisionTable":
        rules = spec.get("rules")
        for index, rule in enumerate(rules if isinstance(rules, list) else []):
            if isinstance(rule, dict):
                expressions.append((rule.get("when"), f"/spec/rules/{index}/when"))
                expressions.append((rule.get("output"), f"/spec/rules/{index}/output"))
        if "defaultOutput" in spec:
            expressions.append((spec["defaultOutput"], "/spec/defaultOutput"))
    for expression, path in expressions:
        found.extend(_operators(expression, path))
    return sorted(found, key=lambda use: _order(use.path))


def _branches(step: JsonObject, path: str) -> list[tuple[JsonObject, str]]:
    kind = step.get("kind")
    result: list[tuple[JsonObject, str]] = []
    if kind == "switch":
        cases = step.get("cases")
        for index, case in enumerate(cases if isinstance(cases, list) else []):
            if isinstance(case, dict):
                result.append((case, f"{path}/cases/{index}"))
        if isinstance(step.get("default"), dict):
            result.append((cast(JsonObject, step["default"]), path + "/default"))
    elif kind == "parallel" and isinstance(step.get("branches"), dict):
        for name, branch in sorted(cast(JsonObject, step["branches"]).items()):
            if isinstance(branch, dict):
                result.append((branch, pointer_child(path + "/branches", name)))
    elif kind == "forEach" and isinstance(step.get("body"), dict):
        result.append((cast(JsonObject, step["body"]), path + "/body"))
    return result


def _operators(expression: JsonValue, path: str) -> list[UnsupportedUse]:
    found: list[UnsupportedUse] = []
    pending: list[tuple[JsonValue, str]] = [(expression, path)]
    while pending:
        node, location = pending.pop()
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("op"), dict):
            operation = cast(JsonObject, node["op"])
            name = operation.get("name")
            feature = OPERATOR_FEATURES.get(name) if isinstance(name, str) else None
            if feature is not None and feature not in COMPILED_FEATURES:
                found.append(UnsupportedUse(location + "/op/name", f"The {name} operator", feature))
            arguments = operation.get("args")
            for index, argument in enumerate(arguments if isinstance(arguments, list) else []):
                pending.append((argument, f"{location}/op/args/{index}"))
        elif isinstance(node.get("object"), dict):
            for key, child in cast(JsonObject, node["object"]).items():
                pending.append((child, pointer_child(location + "/object", key)))
        elif isinstance(node.get("array"), list):
            for index, child in enumerate(cast(list[JsonValue], node["array"])):
                pending.append((child, f"{location}/array/{index}"))
    return found


def _order(path: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key: numeric segments compare as numbers, names as text."""
    return tuple((0, int(part)) if part.isascii() and part.isdigit() else (1, part) for part in path.split("/")[1:])
