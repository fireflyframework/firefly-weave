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
to 64k, full above or when no size is known; an Ollama endpoint without a size is sized as 8,192 tokens (compact).
The input budget is the context minus the answer allowance, at most 4,096 tokens for compact. The gateway's own
text, the reply format and the guide come first, at most a third of the compact budget; the draft, the attachments
and the conversation then share what is left, each held to at most 40%, 20% and 15% of the budget. A part over its
share is trimmed in that order (the draft becomes an outline, then a run lists fewer steps and attachments are left
out from the last, then the oldest messages), and the trimming is listed in ``trimmed`` so the model can say what
it did not see. Nothing is cut mid-value.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from firefly_weave.compiler.api import validate_authoring
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
GATEWAY_RESERVE_TOKENS = 640
# The guide's own tokens: at most 424 on llama3.2:3b, qwen3:4b and qwen2.5:1.5b (measured); counted as this reserve.
GUIDE_TOKENS = 512
# The request's own wrapping (prompt, context, history and attachments keys) and the list of trimmed parts.
ENVELOPE_TOKENS = 64
STANDARD_DRAFT_BYTES = 32768
STANDARD_ATTACHMENT_BYTES = 32768
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
# A fix request too large to send whole lists at most this many problems and the top-level steps they name.
FIX_PROBLEMS = 10
STEP_PATH = re.compile(r"^/spec/steps/(\d+)(?:/|$)")
GUIDE_VERSION = 2
GUIDE = """\
Weave workflow guide (version 2).
A workflow is YAML: apiVersion weave/v1alpha1, kind Workflow, metadata {name, version}, spec {inputSchema,
outputSchema, steps, output}. Each step has a unique id (lowercase letters, digits, hyphens) and a kind:
`action` (uses name@version, with, connection), `llm` (an AI task: uses weave-agentic-generate@1.0.0, profile,
prompt, context, connection), `transform` (value), `decisionTable` (uses, with), `switch` (cases with when, then
default), `parallel` (branches), `forEach` (items, body), `callWorkflow` (uses, with), `wait` (durationSeconds),
`signal` (name, timeoutSeconds, payloadSchema), `humanTask` (assignment, title, context, formSchema) and `fail`
(code, message). Nested steps sit in a block: {steps, output}.
Expressions: {literal: 1}, {ref: /input/name}, {ref: /steps/<id>/output/field}, {object: {key: expr}},
{array: [expr]}, {op: {name, args}}.
Example of a whole workflow document:
  apiVersion: weave/v1alpha1
  kind: Workflow
  metadata: {name: greet, version: 1.0.0}
  spec:
    inputSchema: {type: object, required: [name], properties: {name: {type: string}}}
    outputSchema: {type: object}
    steps:
      - id: greet
        kind: transform
        value: {object: {text: {ref: /input/name}}}
    output: {ref: /steps/greet/output}
Versions are semantic (1.0.0). A proposal is a whole document of kind workflow, decisionTable, action or
connector, never a fragment. Say what you could not see when the context lists trimmed parts.
"""


# Measured on Ollama 0.40.2 with llama3.2:3b, qwen3:4b and qwen2.5:1.5b: ordinary words take about 4.8 characters per
# token, but identifiers, hex, base64, digits and runs of random letters take as little as one character per token,
# and text outside ASCII (Chinese, emoji) more than one token per character. So a chunk with a digit, a run of more
# than 12 letters or more than one lower-to-upper case switch counts one token per character plus one for the space
# before it; in other chunks a letter counts 0.4 tokens (0.7 inside a run of capitals), a mark in a run of two or
# more punctuation marks one, and a single mark 0.6; a newline or an indent counts one; and each character outside
# ASCII counts its UTF-8 bytes, since a byte-level token covers at least one. Shapes that stay below the models'
# count, none of which ordinary text contains: words of random letters (lowercase up to 1.26 times the estimate,
# capitalized up to 1.30) and long runs of spaces or tabs, which count one token per run.
_CHUNK = re.compile(r"\S+|\n|[ \t]{2,}")
_LONG_LETTERS = re.compile(r"[A-Za-z]{13,}")
_CASE_SWITCH = re.compile(r"[a-z][A-Z]")
_CAPITALS = re.compile(r"[A-Z]{2,}")
_MARKS = re.compile(r"[^A-Za-z0-9\s]{2,}")


def estimate_tokens(*parts: str) -> int:
    """A token count never below what the recommended local models count; the same rule as the gateway's."""
    total = 0
    for chunk in _CHUNK.findall("".join(parts)):
        if chunk.isspace():
            total += 1
            continue
        text = chunk.encode("ascii", "ignore").decode()
        total += len(chunk.encode()) - len(text)
        if any(c.isdigit() for c in text) or _LONG_LETTERS.search(text) or len(_CASE_SWITCH.findall(text)) > 1:
            total += len(text) + 1
        else:
            letters = sum(c.isalpha() for c in text)
            capitals = sum(len(run) for run in _CAPITALS.findall(text))
            marks = sum(len(run) for run in _MARKS.findall(text))
            single = len(text) - letters - marks
            total += marks + -(-((letters - capitals) * 4 + capitals * 7 + single * 6) // 10)
    return total


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
        # A compact profile an administrator chose keeps its cap where the policy records no context size.
        return COMPACT_INPUT_TOKENS if profile == "compact" else None
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
            for key in ("steps", "cases", "default", "branches", "body", "errorPath"):
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


def _diagnosed(document: JsonValue, format: str, source: str, allowance: int | None) -> JsonObject:
    """For a proposal too large to send whole: the platform's problems in it, and the top-level steps they name,
    whole, within half the allowance."""
    result = validate_authoring(source, format=cast(Literal["yaml", "json"], format))
    errors = [item for item in result.diagnostics if item.severity == "error"][:FIX_PROBLEMS]
    problems: list[JsonValue] = [{"code": item.code, "path": item.path, "message": item.message} for item in errors]
    spec = document.get("spec") if isinstance(document, dict) else None
    steps = spec.get("steps") if isinstance(spec, dict) else None
    diagnosed: list[JsonValue] = []
    for item in errors:
        match = STEP_PATH.match(item.path or "")
        if match is None or not isinstance(steps, list) or int(match.group(1)) >= len(steps):
            continue
        step = steps[int(match.group(1))]
        if step in diagnosed:
            continue
        if allowance is not None and _tokens([*diagnosed, step, *problems]) > allowance // 2:
            break
        diagnosed.append(step)
    return {"diagnosed": diagnosed, "problems": problems}


def _shortened_run(resource: JsonValue, room: int, trimmed: list[str]) -> JsonValue:
    """A run attachment over its room lists fewer failed steps (counting the rest), then fewer recent steps; the
    run's own facts are kept. Anything else, or a run that does not fit even so, is returned unchanged."""
    data = resource.get("data") if isinstance(resource, dict) and resource.get("kind") == "run" else None
    if not isinstance(resource, dict) or not isinstance(data, dict) or not isinstance(data.get("failed_steps"), list):
        return resource
    failed = cast(list[JsonValue], data["failed_steps"])
    recent = cast(list[JsonValue], data.get("recent_steps") or [])
    more = data.get("more_failed_steps")
    for listed, latest in [
        *((n, len(recent)) for n in range(len(failed) - 1, -1, -1)),
        *((0, n) for n in range(len(recent) - 1, -1, -1)),
    ]:
        shorter: JsonObject = {**data, "failed_steps": failed[:listed], "recent_steps": recent[len(recent) - latest :]}
        left = len(failed) - listed + (more if isinstance(more, int) else 0)
        if left:
            shorter["more_failed_steps"] = left
        candidate: JsonObject = {**resource, "data": shorter}
        if _tokens(candidate) <= room:
            _note(trimmed, "run_steps")
            return candidate
    return resource


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
# Failed steps listed by name; the rest are counted, so sixty failed iterations still fit a compact request.
FAILED_STEPS = 10
# Worker codes (PAYMENT_DECLINED) and `fail` step codes (resource names); anything else is free text, not sent.
ERROR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
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
        "/failed_step",
        "/failed_step/node_id",
        "/failed_step/instance_key",
        "/failed_step/error_code",
        "/failed_steps",
        "/more_failed_steps",
        "/partial",
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


def run_context(summary: RunSummary, steps: Sequence[StepFact], *, partial: bool = False) -> JsonObject:
    """A run as Weave AI sees it: its run facts only, never inputs, outputs, logs, keys, workers or hosts.

    `partial` says the steps are not all of the run's step facts."""

    def at(value: datetime | None) -> JsonValue:
        return value.isoformat().replace("+00:00", "Z") if value is not None else None

    timed = sorted(
        (item for item in steps if item.scheduled_at or item.started_at or item.ended_at),
        key=lambda item: cast(datetime, item.ended_at or item.started_at or item.scheduled_at),
    )

    def code(value: str | None) -> JsonValue:
        return value if value is not None and ERROR_CODE.fullmatch(value) else None

    failed = [item for item in steps if item.status == "failed"]
    stopped = summary.failed_step
    data: JsonObject = {
        "workflow": {"name": summary.workflow.name, "version": summary.workflow.version},
        "status": summary.status,
        "started_at": at(summary.started_at),
        "ended_at": at(summary.ended_at),
        "duration_ms": summary.duration_ms,
        "active_incidents": summary.active_incidents,
        "failed_step": (
            {"node_id": stopped.node_id, "instance_key": stopped.instance_key, "error_code": code(stopped.error_code)}
            if stopped is not None
            else None
        ),
        "failed_steps": [
            {"node_id": item.node_id, "instance_key": item.instance_key, "error_code": code(item.error_code)}
            for item in failed[:FAILED_STEPS]
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
    if len(failed) > FAILED_STEPS:
        data["more_failed_steps"] = len(failed) - FAILED_STEPS
    if partial:
        data["partial"] = True
    return data


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


# Schema keywords whose values are data, not schemas: an object there may hold a "title" that means something.
_SCHEMA_DATA = frozenset({"const", "enum", "default", "examples"})


def untitled(schema: JsonValue) -> JsonValue:
    """A JSON schema without its ``title`` annotations; property names and data values stay as they are."""
    if isinstance(schema, list):
        return [untitled(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result: JsonObject = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _SCHEMA_DATA:
            result[key] = value
        elif key in {"properties", "patternProperties", "$defs", "dependentSchemas"} and isinstance(value, dict):
            result[key] = {name: untitled(child) for name, child in value.items()}
        else:
            result[key] = untitled(value)
    return result


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
    fix: bool = False,
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
        if profile == "standard":
            # A standard context sends the workflow schema whole; its titles repeat the property names.
            wanted = {name: untitled(value) for name, value in wanted.items()}
        if wanted:
            context["authoringSchemas"] = cast(JsonValue, wanted)
    # The instructions, reply format, guide and schemas are sent whole; the other parts share what they leave,
    # each held to its share of the budget and to its part of the rest, so the whole request stays within it.
    fixed = _tokens({key: value for key, value in context.items() if key != "guide"})
    fixed += GUIDE_TOKENS if "guide" in context else 0
    rest = budget - GATEWAY_RESERVE_TOKENS - ENVELOPE_TOKENS - fixed if budget is not None else None

    def share(part: float) -> int | None:
        if budget is None or rest is None:
            return None
        return max(0, min(int(budget * part), int(rest * part / OTHER_SHARES)))

    if draft is not None:
        format, source = draft
        whole: JsonObject = {"format": format, "source": source}
        fits = share(DRAFT_SHARE)
        small = len(source.encode()) <= STANDARD_DRAFT_BYTES and (fits is None or _tokens(whole) <= fits)
        # A change needs the values it changes: compact sends a draft whole whenever it fits its share (measured on
        # qwen3:4b: 0 of 5 valid changes from an outline, 4 of 5 from the whole draft), and a proposal to fix is
        # sent whole on every profile when it fits. Only a draft over its share is outlined.
        fits_whole = fits is None or _tokens(whole) <= fits
        if profile == "full" or (profile == "standard" and small) or ((fix or profile == "compact") and fits_whole):
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
                extra: JsonObject = _diagnosed(document, format, source, fits) if fix else {}
                room = fits - _tokens(extra) if fits is not None else None
                shown = _bounded(outline(document, selected_step), room, trimmed)
                if shown is None:
                    context["draft"] = {"note": TOO_LARGE_DRAFT, **extra}
                    _note(trimmed, "draft")
                else:
                    context["draft"] = {"outline": shown, **extra}
    allowance = share(ATTACHMENT_SHARE)
    kept: list[JsonValue] = []
    for resource in resources:
        room = allowance - _tokens(kept) if allowance is not None and kept else allowance
        whole_fits = room is None or _tokens(resource) <= room
        if profile == "standard" and len(json.dumps(resource, ensure_ascii=False).encode()) > STANDARD_DRAFT_BYTES:
            whole_fits = False
        if profile != "full" and isinstance(resource, dict) and resource.get("kind") == "draft" and not whole_fits:
            # Like the inline draft, a saved draft goes whole when it fits and as an outline when it does not.
            data = cast(JsonObject, resource["data"])
            _note(trimmed, "outlined")
            # The outline is held to the room the attachment's own wrapping leaves.
            wrapping = _tokens({**resource, "data": {"outline": {}, "revision": data["revision"]}})
            shown = _bounded(
                outline(data["document"], selected_step), room - wrapping if room is not None else None, trimmed
            )
            resource = {
                **resource,
                "data": {"outline": shown, "revision": data["revision"]}
                if shown is not None
                else {"note": TOO_LARGE_DRAFT, "revision": data["revision"]},
            }
        if allowance is not None and _tokens([*kept, resource]) > allowance:
            resource = _shortened_run(resource, allowance - _tokens(kept) if kept else allowance, trimmed)
        kept.append(resource)

    def over() -> bool:
        if allowance is not None and _tokens(kept) > allowance:
            return True
        return profile == "standard" and len(json.dumps(kept, ensure_ascii=False).encode()) > STANDARD_ATTACHMENT_BYTES

    # Over their share (or standard's 32 KiB), attachments are left out from the last.
    while kept and over():
        kept.pop()
        _note(trimmed, "attachments")
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
