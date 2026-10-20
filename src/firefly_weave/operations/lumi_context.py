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
from datetime import datetime
from typing import Literal, cast

from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.lumi import LumiMessage
from firefly_weave.contracts.run_views import RunSummary, StepFact
from firefly_weave.contracts.values import JsonObject, JsonValue

type Profile = Literal["compact", "standard", "full"]
type ProfileChoice = Literal["auto", "compact", "standard", "full"]

COMPACT_INPUT_TOKENS = 4096
# Small models on the compact profile loop more the longer they write; a shorter answer also fails sooner.
COMPACT_MAX_TOKENS = 512
COMPACT_BELOW = 16384
STANDARD_UP_TO = 65536
# The gateway's instructions and the reply format it sends with every request; the gateway tests keep them under it.
GATEWAY_RESERVE_TOKENS = 704
# The request's own wrapping (prompt, context, history and attachments keys) and the list of trimmed parts.
ENVELOPE_TOKENS = 64
STANDARD_DRAFT_BYTES = 32768
HISTORY: dict[Profile, int] = {"compact": 4, "standard": 8, "full": 16}
SCHEMAS: dict[Profile, tuple[str, ...]] = {
    "compact": (),
    "standard": ("workflow",),
    "full": ("workflow", "action", "connector", "decisiontable"),
}
GUIDE_SHARE, DRAFT_SHARE, ATTACHMENT_SHARE, HISTORY_SHARE = 0.25, 0.40, 0.20, 0.15
OTHER_SHARES = DRAFT_SHARE + ATTACHMENT_SHARE + HISTORY_SHARE
UNPARSED = "The draft does not parse yet, so only its errors can be discussed."
TOO_LARGE_DRAFT = "The draft is too large to outline within this model's context."
TOO_LARGE_STEP = "This step is too large to send whole."
# The only metadata an outline keeps; descriptions and labels are free text the budget does not need.
METADATA_KEYS = ("name", "version")
OUTLINE_KEYS = ("id", "kind", "uses")
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


# llama3.2:3b counted up to 1.55 times characters / 4 on Weave AI requests (hex identifiers, Japanese text), so the
# estimate counts 2.5 characters per token and never fewer tokens than the model does. The gateway uses the same rule.
def estimate_tokens(text: str) -> int:
    """A conservative token count: one per two and a half characters, rounded up; the same rule as the gateway's."""
    return -(-len(text) * 2 // 5)


def _tokens(value: JsonValue) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False))


def choose_profile(context_tokens: int | None, chosen: ProfileChoice = "auto") -> Profile:
    if chosen != "auto":
        return chosen
    if context_tokens is None or context_tokens > STANDARD_UP_TO:
        return "full"
    return "compact" if context_tokens < COMPACT_BELOW else "standard"


def output_tokens(profile: Profile, max_tokens: int) -> int:
    """The answer's token limit for a profile: the configured limit, at most 512 on the compact profile."""
    return min(max_tokens, COMPACT_MAX_TOKENS) if profile == "compact" else max_tokens


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


def _entry(step: JsonObject) -> JsonObject:
    return {key: step[key] for key in OUTLINE_KEYS if isinstance(step.get(key), str)}


def _note(trimmed: list[str], part: str) -> None:
    if part not in trimmed:
        trimmed.append(part)


def _bounded(shown: JsonObject, allowance: int | None, trimmed: list[str]) -> JsonObject | None:
    """An outline held to its allowance: the selected step named only, then the step list cut in the middle;
    None when not even the first and last step fit."""
    if allowance is None:
        return shown
    selected = shown.get("selected")
    if isinstance(selected, dict) and _tokens(selected) > allowance // 2:
        shown = {**shown, "selected": {**_entry(selected), "note": TOO_LARGE_STEP}}
        _note(trimmed, "selected_step")
    if _tokens(shown) <= allowance:
        return shown
    steps = cast(list[JsonValue], shown["steps"])
    best: JsonObject | None = None
    low, high = 0, len(steps) // 2
    while low <= high:
        # The largest number of steps kept at each end that still fits, found by bisection.
        kept = (low + high) // 2
        candidate: JsonObject = {
            **shown,
            "steps": [*steps[:kept], f"... {len(steps) - 2 * kept} more steps", *steps[len(steps) - kept :]],
        }
        if _tokens(candidate) <= allowance:
            best, low = candidate, kept + 1
        else:
            high = kept - 1
    _note(trimmed, "steps")
    return best


def outline(document: JsonValue, selected_step: str | None) -> JsonObject:
    """Each step's id, kind and uses, in order (nested steps too), and the selected step whole."""
    spec = document.get("spec") if isinstance(document, dict) else None
    steps = _steps(spec.get("steps") if isinstance(spec, dict) else None)
    metadata = document.get("metadata") if isinstance(document, dict) else None
    shown: JsonObject = {
        "kind": document.get("kind") if isinstance(document, dict) else None,
        "metadata": (
            {key: metadata[key] for key in METADATA_KEYS if isinstance(metadata.get(key), str)}
            if isinstance(metadata, dict)
            else None
        ),
        "steps": [_entry(step) for step in steps],
    }
    chosen = next((step for step in steps if step["id"] == selected_step), None)
    if chosen is not None:
        shown["selected"] = chosen
    return shown


RECENT_STEPS = 5
# Every path a run attachment may contain; a test spies on the request and fails on any other.
RUN_FIELDS = frozenset(
    {
        "/workflow",
        "/workflow/name",
        "/workflow/version",
        "/status",
        "/started_at",
        "/ended_at",
        "/duration_ms",
        "/active_incidents",
        "/failed_steps",
        "/failed_steps/*/node_id",
        "/failed_steps/*/instance_key",
        "/failed_steps/*/error_code",
        "/recent_steps",
        "/recent_steps/*/node_id",
        "/recent_steps/*/kind",
        "/recent_steps/*/status",
        "/recent_steps/*/at",
    }
)


def run_context(summary: RunSummary, steps: Sequence[StepFact]) -> JsonObject:
    """A run as Weave AI sees it: its run facts only, never inputs, outputs, logs, keys, workers or hosts."""

    def at(value: datetime | None) -> JsonValue:
        return value.isoformat().replace("+00:00", "Z") if value is not None else None

    timed = sorted(
        (item for item in steps if item.scheduled_at or item.started_at or item.ended_at),
        key=lambda item: cast(datetime, item.ended_at or item.started_at or item.scheduled_at),
    )
    return {
        "workflow": {"name": summary.workflow.name, "version": summary.workflow.version},
        "status": summary.status,
        "started_at": at(summary.started_at),
        "ended_at": at(summary.ended_at),
        "duration_ms": summary.duration_ms,
        "active_incidents": summary.active_incidents,
        "failed_steps": [
            {"node_id": item.node_id, "instance_key": item.instance_key, "error_code": item.error_code}
            for item in steps
            if item.status == "failed"
        ],
        "recent_steps": [
            {
                "node_id": item.node_id,
                "kind": item.kind,
                "status": item.status,
                "at": at(item.ended_at or item.started_at or item.scheduled_at),
            }
            for item in timed[-RECENT_STEPS:]
        ],
    }


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
    # The instructions, reply format, guide and schemas are sent whole; the other parts share what they leave,
    # each held to its share of the budget and to its part of the rest, so the whole request stays within it.
    rest = budget - GATEWAY_RESERVE_TOKENS - ENVELOPE_TOKENS - _tokens(context) if budget is not None else None

    def share(part: float) -> int | None:
        if budget is None or rest is None:
            return None
        return max(0, min(int(budget * part), int(rest * part / OTHER_SHARES)))

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
                trimmed.append("outlined")
                metadata = document.get("metadata") if isinstance(document, dict) else None
                if isinstance(metadata, dict) and set(metadata) - set(METADATA_KEYS):
                    _note(trimmed, "metadata")
                shown = _bounded(outline(document, selected_step), fits, trimmed)
                if shown is None:
                    context["draft"] = {"note": TOO_LARGE_DRAFT}
                    _note(trimmed, "draft")
                else:
                    context["draft"] = {"outline": shown}
    allowance = share(ATTACHMENT_SHARE)
    kept: list[JsonValue] = []
    for resource in resources:
        if profile != "full" and isinstance(resource, dict) and resource.get("kind") == "draft":
            # A saved draft is outlined like the inline one: its own steps, never their values.
            data = cast(JsonObject, resource["data"])
            resource = {
                **resource,
                "data": {"outline": outline(data["document"], selected_step), "revision": data["revision"]},
            }
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
