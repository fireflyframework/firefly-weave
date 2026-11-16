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

"""The agent log: what one AI agent activation did, turn by turn, as `runs.agent_log` answers it.

`runs.agent_log` is `GET {environment}/runs/{identifier}/agents/{key}/log`. ``key`` is an agent step's ID, which pages
through the step's activations in iteration order, or one activation's instance key, such as ``support[2]``. The log
is assembled from the run's recorded facts on every read and never stored.

Values follow the run views' rules. A tool's arguments are classified by the parameters schema the model saw, a tool's
output by the tool's own output schemas, and the final answer by the step's result schema; a value that can't be shown
is left out with its reason. A tool name is the model's own text and is shown escaped (``shown_name``). Times are the
step facts' times, so a turn or a tool call that has not started yet has none.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.definitions import NAME_PATTERN, ContractModel, ResourceName
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, split_instance
from firefly_weave.contracts.values import MAX_SAFE_INTEGER
from firefly_weave.operations.redaction import SafeProjection

# A page holds at most this many activations: a loop of many agents is read a page at a time.
MAX_AGENT_LOG_PAGE: Final = 20
# A model-written tool name is shown in at most this many characters, escapes included.
SHOWN_NAME_LENGTH: Final = 256
_INDEX = r"(?:0|[1-9][0-9]{0,15})"
# An agent step's ID, or one of its activations: the step ID and its loop iterations, outer loop first.
AGENT_LOG_KEY_PATTERN: Final = rf"{NAME_PATTERN}(?:\[{_INDEX}\])*"
_AGENT_LOG_KEY = re.compile(AGENT_LOG_KEY_PATTERN)

type AgentLogKeyText = Annotated[str, Field(min_length=1, max_length=512, pattern=rf"^(?:{AGENT_LOG_KEY_PATTERN})$")]
type Cursor = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9_-]+$")]
# A type alias, so the bounds reach the documented query parameter.
type PageLimit = Annotated[int, Field(ge=1, le=MAX_AGENT_LOG_PAGE)]
type Count = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
type ErrorCode = Annotated[str, Field(min_length=1, max_length=128)]
type AgentStatus = Literal["running", "waiting", "succeeded", "failed", "cancelled", "timed_out"]
# queued: accepted, waiting for room among the parallel calls. review: waiting for a person's review. running: sent
# to its target. succeeded, failed: the target's outcome as the model read it. refused: the platform refused the call
# (an unknown tool, invalid arguments, a repeated call, no tool calls left) and nothing ran. rejected, expired: the
# review said no or nobody answered in time. not_run: the agent stopped before the call could run. cancelled: the call
# was sent but closed when the agent or the run ended.
type CallStatus = Literal[
    "queued", "review", "running", "succeeded", "failed", "refused", "rejected", "expired", "not_run", "cancelled"
]


def agent_log_key(value: object) -> str:
    """``value`` when it names an agent step or one of its activations, else ``ValueError``.

    A turn, tool call or review key (``support#1.1``), a loop yield (``each~1``) and the platform's own ``@`` nodes
    are not activations.
    """
    if type(value) is not str or len(value) > 512 or _AGENT_LOG_KEY.fullmatch(value) is None:
        raise ValueError("An agent log key is a step ID or an activation's instance key")
    try:
        split_instance(value)
    except InvalidInstanceKey:
        raise ValueError("An agent log key is a step ID or an activation's instance key") from None
    return value


def shown_name(text: str) -> str:
    """A model-written tool name as every surface shows it: format characters dropped, control characters and line
    separators written as ``\\uXXXX`` (a backslash as ``\\\\``), at most ``SHOWN_NAME_LENGTH`` characters, never cut
    inside an escape."""
    shown: list[str] = []
    length = 0
    for character in text:
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        if character == "\\":
            piece = "\\\\"
        elif category in {"Cc", "Zl", "Zp"}:
            piece = f"\\u{ord(character):04x}"
        else:
            piece = character
        if length + len(piece) > SHOWN_NAME_LENGTH:
            break
        shown.append(piece)
        length += len(piece)
    return "".join(shown)


class AgentLogQuery(ContractModel):
    """`runs.agent_log` query parameters: a page of at most 20 activations."""

    limit: PageLimit = MAX_AGENT_LOG_PAGE
    cursor: Cursor | None = None

    def cursor_collection(self, run_id: UUID, key: str) -> str:
        digest = hashlib.sha256(json.dumps({"key": key}, separators=(",", ":")).encode()).hexdigest()
        return f"runs.agent_log:{run_id}:{digest}"


class AgentToolTarget(ContractModel):
    """What a tool runs: its kind (``action``, ``workflow`` or ``human`` today) and the pinned action's or workflow's
    reference, or null for a person."""

    # Published as text, so a new kind of tool reaches clients without a contract change; a client shows an unknown
    # kind by its reference only.
    kind: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    reference: Annotated[str, Field(min_length=1, max_length=512)] | None


class AgentError(ContractModel):
    code: ErrorCode


class AgentCallReview(ContractModel):
    """A person's review of a call that changes data: approve, reject (with an optional comment), or expired."""

    decision: Literal["approve", "reject", "expired"]
    comment: Annotated[str, Field(max_length=1024)] | None


class AgentCall(ContractModel):
    """One tool call the model asked for, in the order it wrote them."""

    instance_key: Annotated[str, Field(min_length=1, max_length=512)]
    # The model's text, escaped by ``shown_name``.
    name: Annotated[str, Field(max_length=SHOWN_NAME_LENGTH)]
    # Null when the name matches none of the step's tools.
    target: AgentToolTarget | None
    status: CallStatus
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    arguments: SafeProjection
    # Null until the target answers, and for a call that never ran.
    output: SafeProjection | None
    error: AgentError | None
    review: AgentCallReview | None
    # The child run a workflow tool started.
    child_run_id: UUID | None


class AgentTurn(ContractModel):
    """One model turn: its tokens, its time, the calls the model asked for, or its final answer."""

    turn: Annotated[int, Field(ge=1, le=9999)]
    # The forced final turn: the model could call no tools and had to answer.
    forced: bool
    input_tokens: Count | None
    output_tokens: Count | None
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    # The final answer's result, when this turn answered.
    final: SafeProjection | None
    calls: list[AgentCall] = Field(max_length=16)
    # Calls past the 16 a turn keeps; the model asked for them and none ran.
    dropped_tool_calls: Count
    error: AgentError | None


class AgentMemoryUse(ContractModel):
    """The agent's memory scope and how many remembered messages its first turn carried."""

    scope: Literal["step", "run", "conversation"]
    loaded: Count


class AgentTotals(ContractModel):
    turns: Count
    tool_calls: Count
    input_tokens: Count
    output_tokens: Count
    # From the activation's start to its end; null while it runs.
    duration_ms: Count | None


class AgentLog(ContractModel):
    """One AI agent activation, turn by turn."""

    node_id: ResourceName
    instance_key: AgentLogKeyText
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    status: AgentStatus
    # How a succeeded agent stopped: its own final answer, or the forced final turn.
    stop: Literal["final", "forced_final"] | None
    # Why a failed agent failed.
    error: AgentError | None
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    memory: AgentMemoryUse
    turns: list[AgentTurn] = Field(max_length=64)
    totals: AgentTotals


class AgentLogPage(ContractModel):
    items: list[AgentLog] = Field(max_length=MAX_AGENT_LOG_PAGE)
    next_cursor: Cursor | None = None
