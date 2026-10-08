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

"""The template view of a ``concat`` tree: the reference mapping that Studio's template editor follows.

A template is never stored as template text. Text segments are the ``{literal: <string>}`` arguments of a ``concat``
and every other argument is a placeholder, in order. Studio prints placeholders in its own formula syntax
(``Hi {{ item.name }}``) and escapes literal braces; that syntax belongs to Studio, not to this module.
"""

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from firefly_weave.contracts.values import JsonObject, JsonValue


@dataclass(frozen=True)
class Segment:
    """One part of a template: ``text`` for a text segment, ``expression`` for a placeholder."""

    kind: Literal["text", "placeholder"]
    text: str = ""
    expression: JsonObject | None = None

    def to_json(self) -> JsonObject:
        if self.kind == "text":
            return {"kind": "text", "text": self.text}
        return {"kind": "placeholder", "expression": copy.deepcopy(self.expression)}


def _text(argument: JsonValue) -> str | None:
    if isinstance(argument, dict) and len(argument) == 1 and isinstance(argument.get("literal"), str):
        return cast(str, argument["literal"])
    return None


def segments(expression: JsonValue) -> list[Segment] | None:
    """The segments of a ``concat`` expression, one per argument; ``None`` for any other expression.

    Any ``concat`` renders as a template. A string literal argument is text; any other argument, including a
    number or Boolean literal, is a placeholder (``{{ 42 }}``). Other expressions keep their own editor.
    """
    if not isinstance(expression, dict) or set(expression) != {"op"}:
        return None
    operation = expression["op"]
    if not isinstance(operation, dict) or operation.get("name") != "concat":
        return None
    arguments = operation.get("args")
    if not isinstance(arguments, list):
        return None
    result: list[Segment] = []
    for argument in arguments:
        text = _text(argument)
        if text is not None:
            result.append(Segment("text", text=text))
        else:
            result.append(Segment("placeholder", expression=cast(JsonObject, copy.deepcopy(argument))))
    return result


def template_expression(parts: Sequence[Segment]) -> JsonObject:
    """The expression a template saves as: adjacent text merged, empty text dropped, and plain text a literal."""
    arguments: list[JsonValue] = []
    for part in parts:
        if part.kind == "placeholder":
            arguments.append(copy.deepcopy(cast(JsonObject, part.expression)))
        elif part.text:
            previous = _text(arguments[-1]) if arguments else None
            if previous is None:
                arguments.append({"literal": part.text})
            else:
                arguments[-1] = {"literal": previous + part.text}
    if all(_text(argument) is not None for argument in arguments):
        return {"literal": "".join(cast(str, _text(argument)) for argument in arguments)}
    return {"op": {"name": "concat", "args": arguments}}
