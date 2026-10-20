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

"""What Weave AI sends a model, sized to its context.

A profile follows the model's context size (or the administrator's choice): compact under 16k tokens, standard up
to 64k, full above. The input budget is the context minus the answer allowance, at most 4,096 tokens for compact.
It is split: the gateway's own text, the reply format and the guide take at most a quarter, the draft 40%, the
attachments 20% and the conversation 15%. A part over its share is trimmed in that order (the draft becomes an
outline, then attachments are left out from the last, then the oldest messages), and the trimming is listed in
``trimmed`` so the model can say what it did not see. Nothing is cut mid-value.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.lumi import LumiMessage
from firefly_weave.contracts.values import JsonObject, JsonValue

type Profile = Literal["compact", "standard", "full"]
type ProfileChoice = Literal["auto", "compact", "standard", "full"]

COMPACT_INPUT_TOKENS = 4096
COMPACT_BELOW = 16384
STANDARD_UP_TO = 65536
# The gateway's instructions and the reply format it sends with every request; the gateway tests keep them under it.
GATEWAY_RESERVE_TOKENS = 512
STANDARD_DRAFT_BYTES = 32768
HISTORY: dict[Profile, int] = {"compact": 4, "standard": 8, "full": 16}
SCHEMAS: dict[Profile, tuple[str, ...]] = {
    "compact": (),
    "standard": ("workflow",),
    "full": ("workflow", "action", "connector", "decisiontable"),
}
GUIDE_SHARE, DRAFT_SHARE, ATTACHMENT_SHARE, HISTORY_SHARE = 0.25, 0.40, 0.20, 0.15
UNPARSED = "The draft does not parse yet, so only its errors can be discussed."
GUIDE_VERSION = 1
GUIDE = """\
Weave workflow guide (version 1).
A workflow is YAML: apiVersion weave/v1alpha1, kind Workflow, metadata {name, version}, spec {inputSchema,
outputSchema, steps, output}. Each step has a unique id (lowercase letters, digits, hyphens) and a kind:
`action` (uses name@version, with, connection), `llm` (an AI task: uses weave-agentic-generate@1.0.0, profile,
prompt, context, connection), `transform` (value), `decisionTable` (uses, with), `switch` (cases with when, then
default), `parallel` (branches), `forEach` (items, body), `callWorkflow` (uses, with), `wait` (durationSeconds),
`signal` (name, timeoutSeconds, payloadSchema), `humanTask` (assignment, title, context, formSchema) and `fail`
(code, message). Nested steps sit in a block: {steps, output}.
Expressions: {literal: 1}, {ref: /input/name}, {ref: /steps/<id>/output/field}, {object: {key: expr}},
{array: [expr]}, {op: {name, args}}.
Example:
  steps:
    - id: greet
      kind: transform
      value: {object: {text: {ref: /input/name}}}
  output: {ref: /steps/greet/output}
Versions are semantic (1.0.0). A proposal is a whole document of kind workflow, decisionTable, action or
connector, never a fragment. Say what you could not see when the context lists trimmed parts.
"""


def estimate_tokens(text: str) -> int:
    """An approximate token count, one per four characters, rounded up; the same rule as the gateway's."""
    return -(-len(text) // 4)


def _tokens(value: JsonValue) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False))


def choose_profile(context_tokens: int | None, chosen: ProfileChoice = "auto") -> Profile:
    if chosen != "auto":
        return chosen
    if context_tokens is None or context_tokens > STANDARD_UP_TO:
        return "full"
    return "compact" if context_tokens < COMPACT_BELOW else "standard"


def input_budget(profile: Profile, context_tokens: int | None, max_tokens: int) -> int | None:
    """Tokens the request may use, or None when the context size is unknown (cloud endpoints)."""
    if context_tokens is None:
        return None
    budget = context_tokens - max_tokens
    return min(budget, COMPACT_INPUT_TOKENS) if profile == "compact" else budget


def _steps(block: object) -> list[JsonObject]:
    found: list[JsonObject] = []
    pending: list[object] = [block]
    while pending:
        value = pending.pop(0)
        if isinstance(value, list):
            pending[:0] = value
        elif isinstance(value, dict):
            if isinstance(value.get("id"), str) and isinstance(value.get("kind"), str):
                found.append(cast(JsonObject, value))
            for key in ("steps", "cases", "default", "branches", "body"):
                child = value.get(key)
                pending.extend(child.values() if key == "branches" and isinstance(child, dict) else [child])
    return found


def outline(document: JsonValue, selected_step: str | None) -> JsonObject:
    """Each step's id, kind and uses, in order (nested steps too), and the selected step whole."""
    spec = document.get("spec") if isinstance(document, dict) else None
    steps = _steps(spec.get("steps") if isinstance(spec, dict) else None)
    shown: JsonObject = {
        "kind": document.get("kind") if isinstance(document, dict) else None,
        "metadata": document.get("metadata") if isinstance(document, dict) else None,
        "steps": [
            {key: step[key] for key in ("id", "kind", "uses") if isinstance(step.get(key), str)} for step in steps
        ],
    }
    chosen = next((step for step in steps if step["id"] == selected_step), None)
    if chosen is not None:
        shown["selected"] = chosen
    return shown


@dataclass(frozen=True)
class Shaped:
    """The attachments object the gateway sends as context, and the conversation that fits."""

    context: JsonObject
    history: list[LumiMessage]
    profile: Profile


def _parsed(format: str, source: str) -> JsonValue | None:
    try:
        return parse_source(source, format=cast(Literal["yaml", "json"], format)).value
    except Exception:
        return None


def shape(
    profile: Profile,
    budget: int | None,
    *,
    message: str,
    history: Sequence[LumiMessage],
    resources: list[JsonValue],
    draft: tuple[str, str] | None,
    selected_step: str | None,
    schemas: dict[str, JsonValue],
    explanation_only: bool = False,
) -> Shaped:
    """What to send for one question; deterministic, so the same inputs send the same request."""
    trimmed: list[str] = []
    context: JsonObject = {"profile": profile}
    if explanation_only:
        context["purpose"] = "explain-operations"
        context["limitations"] = (
            "Saved snapshots only, not live cloud state or worker task capacity. Explain only; return no proposals."
        )
    else:
        if profile != "full":
            context["guide"] = GUIDE
        wanted = {name: schemas[name] for name in SCHEMAS[profile] if name in schemas}
        if wanted:
            context["authoringSchemas"] = cast(JsonValue, wanted)
    share = (lambda part: int(budget * part)) if budget is not None else (lambda part: None)
    if draft is not None:
        format, source = draft
        whole: JsonObject = {"format": format, "source": source}
        fits = share(DRAFT_SHARE)
        small = len(source.encode()) <= STANDARD_DRAFT_BYTES and (fits is None or _tokens(whole) <= fits)
        if profile == "full" or (profile == "standard" and small):
            context["draft"] = whole
        else:
            document = _parsed(format, source)
            if document is None:
                context["draft"] = {"note": UNPARSED}
            else:
                context["draft"] = {"outline": outline(document, selected_step)}
                trimmed.append("outlined")
    allowance = share(ATTACHMENT_SHARE)
    kept: list[JsonValue] = []
    for resource in resources:
        if allowance is not None and _tokens([*kept, resource]) > allowance:
            if "attachments" not in trimmed:
                trimmed.append("attachments")
            continue
        kept.append(resource)
    context["resources"] = kept
    room = share(HISTORY_SHARE)
    if room is not None:
        room -= estimate_tokens(message)
    recent = list(history)[-HISTORY[profile] :]
    while recent and room is not None and _tokens([item.model_dump() for item in recent]) > room:
        recent.pop(0)
    if len(recent) < min(len(history), HISTORY[profile]):
        trimmed.append("history")
    if trimmed:
        context["trimmed"] = cast(JsonValue, trimmed)
    return Shaped(context=context, history=recent, profile=profile)
