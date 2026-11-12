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

"""The agent log behind `run.read`: what an AI agent did, assembled on read from what its run recorded.

Nothing is stored for the log. ``assemble`` is pure: it reads the pinned IR, the step facts of the agent's keys (times,
status, error codes), the outputs its turns, calls and reviews recorded, and what each turn's task recorded about its
input, and returns the log. Every read names the run. Outputs, turn inputs and agent state are read by their exact keys.
Step facts are read by run and step and narrowed to the page's keys in the database, and a step's activations are sorted
by their loop indexes there, so in a loop of many agents those two reads grow with the step's facts in the run; what a
page returns stays bounded. The values on a page are limited to ``PAGE_BYTES``: turns before calls, the smallest first;
a value that doesn't fit is left out with the ``resource_limit`` omission, and its turn keeps its shape.

A call's tool is the one whose name the model wrote exactly, and everything about the tool goes through its target, so
a new kind of tool needs no change here. A completed call's outcome is the result the model read: the next turn's input
carries it, or, while the agent runs, its state. A call the platform refused never ran and has no key of its own; the
reason the model read is found the same way. Values are classified with ``operations.redaction.project``, the run
views' classifier, and a model-written tool name is escaped by ``contracts.agent_log.shown_name``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.compiler.ir import AgentNode, AgentToolIR, ToolTargetKind, WorkflowIR
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_log import (
    MAX_AGENT_LOG_PAGE,
    AgentCall,
    AgentCallReview,
    AgentError,
    AgentLog,
    AgentLogPage,
    AgentLogQuery,
    AgentMemoryUse,
    AgentStatus,
    AgentToolTarget,
    AgentTotals,
    AgentTurn,
    CallStatus,
    agent_log_key,
    shown_name,
)
from firefly_weave.contracts.agent_turns import MAX_TURN_TOOL_CALLS, CalledName, TurnOutput, TurnUsage, call_id
from firefly_weave.contracts.instance_keys import node_of, split_instance
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import Omission, SafeProjection, project
from firefly_weave.persistence.paging import PAGE_BYTES
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.agent_tools import TOOL_TARGETS, tool_schema
from firefly_weave.runtime.agents import call_key, parse_agent_key, review_key, turn_key
from firefly_weave.runtime.models import AgentState

TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
# The results the platform answers a refused call with; the model read them, and nothing ran.
REFUSALS: Final = frozenset({"UNKNOWN_TOOL", "INVALID_ARGUMENTS", "REPEATED_CALL", "TOOL_BUDGET_EXHAUSTED"})
UNCLASSIFIED: Final = SafeProjection(
    available=False, omissions=[Omission(path="", reason="classification_unavailable")]
)
# A value the page's byte limit left out.
OVER_PAGE_LIMIT: Final = SafeProjection(available=False, omissions=[Omission(path="", reason="resource_limit")])
NOT_FOUND: Final = "No AI agent activation at this key"
# The code a call still running when its run timed out fails with, when its step fact records none.
TIMED_OUT: Final = "TIMEOUT"


@dataclass(frozen=True)
class StepRecord:
    """The step fact of one key: the agent's activation, a turn, a call or a review."""

    key: str
    status: str
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_code: str | None = None
    child_run_id: UUID | None = None


@dataclass(frozen=True)
class RecordedResult:
    """One tool result as the model read it, reduced to what the log shows: whether it is an error and its code."""

    is_error: bool
    error: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class TurnRecord:
    """What a turn's task recorded about its input: tools on or off, the memory it loaded, and the results of earlier
    calls by call ID (``t1c2``)."""

    key: str
    tools_enabled: bool = True
    memory: int = 0
    results: Mapping[str, RecordedResult] = field(default_factory=dict)


class _Called(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    name: CalledName


class _TurnShape(BaseModel):
    """What a page reads of a turn whose values its byte limit left out: its kind, its calls' names and its usage."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", populate_by_name=True)

    kind: Literal["tool_calls", "final"]
    tool_calls: list[_Called] = Field(default_factory=list, max_length=MAX_TURN_TOOL_CALLS, alias="toolCalls")
    usage: TurnUsage
    dropped_tool_calls: int | None = Field(default=None, ge=1, le=MAX_SAFE_INTEGER, alias="droppedToolCalls")


@dataclass(frozen=True)
class _Answer:
    """A turn's output as the log reads it. ``complete`` is false when the page's byte limit left its values out: its
    calls' arguments and its final answer."""

    kind: Literal["tool_calls", "final"]
    calls: list[tuple[str, JsonValue]]
    result: JsonValue
    usage: TurnUsage
    dropped: int
    complete: bool = True


def assemble(
    ir: WorkflowIR,
    key: str,
    *,
    steps: Sequence[StepRecord],
    outputs: Mapping[str, JsonValue],
    intents: Sequence[TurnRecord],
    state: AgentState | None = None,
    withheld: Mapping[str, JsonValue] | None = None,
) -> AgentLog:
    """The log of the agent activation at ``key``.

    ``steps`` are step facts, ``outputs`` the outputs completed keys recorded, ``intents`` what turn tasks recorded
    about their inputs, and ``state`` the agent's state while it runs. ``withheld`` holds the completed keys whose
    values the page's byte limit left out, with a turn's shape (``kind``, ``toolCalls`` names, ``usage``,
    ``droppedToolCalls``) or None for a call. Records of other keys are ignored.
    """
    node = next((item for item in ir.graph.nodes if item.id == node_of(key)), None)
    if not isinstance(node, AgentNode):
        raise ValueError("The key names no AI agent step")
    mine = key + "#"
    facts = {step.key: step for step in steps if step.key == key or step.key.startswith(mine)}
    turns = {record.key: record for record in intents if record.key.startswith(mine)}
    numbers = sorted(
        {
            parsed.turn
            for name in (*facts, *turns)
            if (parsed := parse_agent_key(name)) is not None and parsed.agent == key and parsed.call is None
        }
    )
    activation = facts.get(key)
    active = activation.status not in TERMINAL if activation is not None else state is not None
    context = _Context(ir, node, key, facts, outputs, withheld or {}, turns, state if active else None, active)
    rows = [context.turn(number) for number in numbers]
    status = _status(activation, rows)
    last = rows[-1] if rows else None
    stop: Literal["final", "forced_final"] | None = None
    if status == "succeeded" and last is not None and last.final is not None:
        stop = "forced_final" if last.forced else "final"
    started = activation.scheduled_at if activation is not None else None
    ended = activation.ended_at if activation is not None and status in TERMINAL else None
    first = turns.get(turn_key(key, numbers[0])) if numbers else None
    return AgentLog(
        node_id=node.id,
        instance_key=key,
        provider=node.llm_profile.provider,
        model=node.llm_profile.model,
        status=status,
        stop=stop,
        error=_error(activation.error_code) if activation is not None and status == "failed" else None,
        started_at=started,
        ended_at=ended,
        memory=AgentMemoryUse(scope=node.memory.scope, loaded=first.memory if first is not None else 0),
        turns=rows,
        totals=AgentTotals(
            turns=len(rows),
            tool_calls=sum(len(row.calls) for row in rows),
            input_tokens=sum(row.input_tokens or 0 for row in rows),
            output_tokens=sum(row.output_tokens or 0 for row in rows),
            duration_ms=_milliseconds(started, ended),
        ),
    )


@dataclass(frozen=True)
class _Context:
    ir: WorkflowIR
    node: AgentNode
    key: str
    facts: Mapping[str, StepRecord]
    outputs: Mapping[str, JsonValue]
    withheld: Mapping[str, JsonValue]
    turns: Mapping[str, TurnRecord]
    state: AgentState | None
    active: bool

    def turn(self, number: int) -> AgentTurn:
        name = turn_key(self.key, number)
        fact = self.facts.get(name)
        answer = self.answer(name)
        record = self.turns.get(name)
        calls = (
            [
                self.call(number, index, called, arguments, answer.complete)
                for index, (called, arguments) in enumerate(answer.calls, start=1)
            ]
            if answer is not None
            else []
        )
        final = None
        if answer is not None and answer.kind == "final":
            final = (
                project(answer.result, self.node.llm_profile.output_schema, _bundle(self.ir))
                if answer.complete
                else OVER_PAGE_LIMIT
            )
        started, ended = _times(fact)
        return AgentTurn(
            turn=number,
            forced=record is not None and not record.tools_enabled,
            input_tokens=answer.usage.input_tokens if answer is not None else None,
            output_tokens=answer.usage.output_tokens if answer is not None else None,
            started_at=started,
            ended_at=ended,
            final=final,
            calls=calls,
            dropped_tool_calls=answer.dropped if answer is not None else 0,
            error=_error(fact.error_code) if fact is not None and fact.status == "failed" else None,
        )

    def answer(self, name: str) -> _Answer | None:
        """A turn's recorded output, or only its shape when the page's byte limit left its values out."""
        if name in self.outputs:
            output = _turn_output(self.outputs[name])
            if output is None:
                return None
            return _Answer(
                kind=output.kind,
                calls=[(call.name, call.arguments) for call in output.tool_calls],
                result=output.result,
                usage=output.usage,
                dropped=output.dropped_tool_calls or 0,
            )
        if name not in self.withheld:
            return None
        try:
            shape = _TurnShape.model_validate(self.withheld[name])
        except ValidationError:
            return None
        return _Answer(
            kind=shape.kind,
            calls=[(call.name, None) for call in shape.tool_calls],
            result=None,
            usage=shape.usage,
            dropped=shape.dropped_tool_calls or 0,
            complete=False,
        )

    def call(self, turn: int, index: int, called: str, written: JsonValue, complete: bool) -> AgentCall:
        """The call the model wrote as ``called`` with the arguments ``written``; ``complete`` is false when the page's
        byte limit left its turn's values out."""
        name = call_key(self.key, turn, index)
        # The model's text names the tool exactly, byte for byte, as the kernel matched it.
        tool = next((item for item in self.node.tools if item.name == called), None)
        if not complete:
            arguments = OVER_PAGE_LIMIT
        elif tool is not None and isinstance(written, dict):
            arguments = project(written, tool_schema(self.ir, tool), _bundle(self.ir))
        else:
            arguments = UNCLASSIFIED
        asked = review_key(self.key, turn, index)
        review_fact = self.facts.get(asked)
        review = _review(self.outputs.get(asked), review_fact)
        fact = self.facts.get(name)

        def made(
            status: CallStatus,
            timed: StepRecord | None,
            *,
            error: AgentError | None = None,
            output: SafeProjection | None = None,
        ) -> AgentCall:
            started, ended = _times(timed)
            return AgentCall(
                instance_key=name,
                name=shown_name(called),
                target=_target(self.ir, tool),
                status=status,
                started_at=started,
                ended_at=ended,
                arguments=arguments,
                output=output,
                error=error,
                review=review,
                child_run_id=fact.child_run_id if fact is not None else None,
            )

        if review is not None and review.decision != "approve":
            return made("rejected" if review.decision == "reject" else "expired", review_fact)
        if fact is not None:
            if fact.status == "failed":
                return made("failed", fact, error=_error(fact.error_code))
            if fact.status == "timed_out":
                # Still running when its run timed out: the call failed, with the timeout's code.
                return made("failed", fact, error=_error(fact.error_code or TIMED_OUT))
            if fact.status == "succeeded":
                result = self.result(turn, index, name)
                status: CallStatus = "succeeded"
                error = None
                if result is not None and result.is_error:
                    status = "expired" if result.status == "expired" else "failed"
                    error = _error(result.error) if status == "failed" else None
                output = None
                if tool is not None and name in self.outputs:
                    target = TOOL_TARGETS[tool.target]
                    # What replay screens a completion's output with, for the outcome the model read.
                    completion = {"status": "succeeded" if status == "succeeded" else "failed"}
                    schemas = target.event_schemas(self.ir, tool, target.completion, completion)
                    output = _classified(self.outputs[name], schemas, _bundle(self.ir))
                elif tool is not None and name in self.withheld:
                    output = OVER_PAGE_LIMIT
                return made(status, fact, error=error, output=output)
            return made("running" if fact.status not in TERMINAL and self.active else "cancelled", fact)
        if self.queued(name):
            # Accepted, or approved after its review, and waiting for room: it has no key of its own yet.
            return made("queued", None)
        if review_fact is not None:
            return made("review" if review_fact.status not in TERMINAL and self.active else "cancelled", review_fact)
        status, code = self.unsent(turn, index, name)
        return made(status, None, error=_error(code))

    def queued(self, name: str) -> bool:
        """Whether the running agent holds the call at ``name`` until there is room among its parallel calls."""
        return self.state is not None and any(
            record.key == name and record.status == "queued" for record in self.state.calls
        )

    def result(self, turn: int, index: int, name: str) -> RecordedResult | None:
        """The result the model read for a call: from the agent's state while it runs, else from the next turn."""
        if self.state is not None and name in self.state.results:
            message = self.state.results[name]
            content = message.get("content")
            return RecordedResult(
                is_error=message.get("isError") is True,
                error=_text(content.get("error")) if isinstance(content, dict) else None,
                status=_text(content.get("status")) if isinstance(content, dict) else None,
            )
        following = self.turns.get(turn_key(self.key, turn + 1))
        return following.results.get(call_id(turn, index)) if following is not None else None

    def unsent(self, turn: int, index: int, name: str) -> tuple[CallStatus, str | None]:
        """A call with no key of its own that isn't waiting: refused with the code the model read, or never run."""
        found = self.result(turn, index, name)
        if found is not None and found.is_error and found.error in REFUSALS:
            return "refused", found.error
        return "not_run", None


def _status(activation: StepRecord | None, rows: list[AgentTurn]) -> AgentStatus:
    if activation is not None and activation.status in TERMINAL:
        return cast(AgentStatus, activation.status)
    # A running agent waits while a person must act: a review, or a running tool that asks a person.
    for row in rows:
        for call in row.calls:
            target = TOOL_TARGETS.get(cast(ToolTargetKind, call.target.kind)) if call.target is not None else None
            if call.status == "review" or (
                call.status == "running" and target is not None and target.completion == "human_completed"
            ):
                return "waiting"
    return "running"


def _turn_output(value: JsonValue) -> TurnOutput | None:
    try:
        return TurnOutput.model_validate(value)
    except ValidationError:
        return None


def _review(output: JsonValue, fact: StepRecord | None) -> AgentCallReview | None:
    """A review's answer; a review that ended with no answer expired."""
    if isinstance(output, dict) and output.get("decision") in {"approve", "reject"}:
        data = output.get("data")
        comment = data.get("comment") if isinstance(data, dict) else None
        decision: Literal["approve", "reject"] = "approve" if output["decision"] == "approve" else "reject"
        return AgentCallReview(decision=decision, comment=comment if isinstance(comment, str) else None)
    if fact is not None and fact.status == "succeeded":
        return AgentCallReview(decision="expired", comment=None)
    return None


def _target(ir: WorkflowIR, tool: AgentToolIR | None) -> AgentToolTarget | None:
    if tool is None:
        return None
    reference = next((item.reference for item in ir.dependencies if item.digest == tool.dependency), None)
    return AgentToolTarget(kind=tool.target, reference=reference)


def _classified(value: JsonValue, schemas: list[JsonObject], bundle: dict[str, JsonObject]) -> SafeProjection:
    """``value`` shown only when every schema it may have been checked with lets it be shown."""
    if not schemas:
        return UNCLASSIFIED
    projections = [project(value, schema, bundle) for schema in schemas]
    return next((projection for projection in projections if not projection.available), projections[0])


def _bundle(ir: WorkflowIR) -> dict[str, JsonObject]:
    return {item.reference: item.document for item in ir.dependencies if item.kind == "Schema"}


def _times(fact: StepRecord | None) -> tuple[datetime | None, datetime | None]:
    """When a key started (its claim, else its schedule) and, once it ended, when."""
    if fact is None:
        return None, None
    return fact.started_at or fact.scheduled_at, fact.ended_at if fact.status in TERMINAL else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _error(code: str | None) -> AgentError | None:
    return AgentError(code=code) if isinstance(code, str) and 0 < len(code) <= 128 else None


def _milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def iteration(key: str) -> list[int]:
    """An activation's loop indexes, outer loop first: the order a step's activations are listed in."""
    return list(split_instance(key).indexes)


_STEP_SCOPE = "f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment AND f.run_id=:run"
# An activation's loop indexes as numbers: "" (the step outside a loop) is {}, "support[1][3]" is {1,3}.
_INDEXES = (
    "coalesce(string_to_array(nullif(btrim(substr(f.instance_key,length(f.node_id)+1),'[]'),''),'][')::numeric[],"
    "'{}'::numeric[])"
)


class AgentLogRepository:
    """Reads of what a run recorded for its agents: every read names the run, and the step or the keys it reads."""

    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        result = await self.tx.session.execute(text(sql), {**self.params, **values})
        return [dict(row) for row in result.mappings()]

    async def workflow(self, run_id: UUID) -> WorkflowIR:
        """The run's pinned IR, once the run is known and its state may be shown; 404 for an unknown run."""
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.operations.fact_reads import FactReadRepository
        from firefly_weave.runtime import admission
        from firefly_weave.runtime.kernel import workflow

        await FactReadRepository(self.tx).metadata(run_id)
        rows = await self.rows(
            "SELECT r.artifact,jsonb_build_object('unavailable',r.state->'unavailable','admission_policy',"
            "r.state->'admission_policy') AS state FROM runs r WHERE r.tenant_id=:tenant AND r.project_id=:project "
            "AND r.environment_id=:environment AND r.id=:run",
            run=run_id,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
        admission.require_available(rows[0])
        return workflow(import_artifact(rows[0]["artifact"]))

    async def activations(self, run_id: UUID, step: str, after: list[int] | None, limit: int) -> list[str]:
        """The step's activations in iteration order, after ``after``: the step itself outside a loop. The database
        sorts every activation of the step in the run by its loop indexes."""
        rows = await self.rows(
            f"SELECT f.instance_key FROM step_facts f WHERE {_STEP_SCOPE} AND f.node_id=:step "
            f"AND f.instance_key !~ '[#~]' AND (cast(:after AS numeric[]) IS NULL OR {_INDEXES}>cast(:after AS "
            f"numeric[])) ORDER BY {_INDEXES} LIMIT :limit",
            run=run_id,
            step=step,
            after=after,
            limit=limit,
        )
        return [row["instance_key"] or step for row in rows]

    async def steps(self, run_id: UUID, step: str, keys: list[str]) -> list[StepRecord]:
        """The facts of the activations at ``keys`` and of every turn, call and review under them, matched among the
        step's facts in the run."""
        rows = await self.rows(
            "SELECT f.instance_key,f.status,f.scheduled_at,f.started_at,f.ended_at,f.error_code,f.child_run_id "
            f"FROM step_facts f WHERE {_STEP_SCOPE} AND f.node_id=:step AND (f.instance_key=ANY(:exact) OR "
            "EXISTS(SELECT 1 FROM unnest(cast(:prefixes AS text[])) AS p(prefix) "
            "WHERE starts_with(f.instance_key,p.prefix)))",
            run=run_id,
            step=step,
            exact=["" if key == step else key for key in keys],
            prefixes=[key + "#" for key in keys],
        )
        return [
            StepRecord(
                key=row["instance_key"] or step,
                status=row["status"],
                scheduled_at=row["scheduled_at"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                error_code=row["error_code"],
                child_run_id=row["child_run_id"],
            )
            for row in rows
        ]

    async def outputs(self, run_id: UUID, keys: list[str]) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        """The outputs the completed keys recorded, and the keys whose values the page's byte limit left out.

        Reviews are small and always read. Turns come next, then calls, the smallest first, while the page's total
        stays within ``PAGE_BYTES``; the rest stay in the database. A turn left out still gives its shape (its kind,
        its calls' IDs and names, its usage), so its calls keep their rows; a call left out gives None.
        """
        if not keys:
            return {}, {}
        rows = await self.rows(
            "WITH sized AS (SELECT s.node_id,s.output,octet_length(s.output::text) AS bytes,"
            "CASE WHEN s.node_id ~ '[.]review$' THEN 0 WHEN s.node_id ~ '#[0-9]+$' THEN 1 ELSE 2 END AS tier "
            "FROM step_instances s WHERE s.tenant_id=:tenant AND s.project_id=:project "
            "AND s.environment_id=:environment AND s.run_id=:run AND s.node_id=ANY(:keys) AND s.status='completed'),"
            "ranked AS (SELECT node_id,output,tier,sum(CASE WHEN tier=0 THEN 0 ELSE bytes END) OVER "
            '(ORDER BY tier,bytes,node_id COLLATE "C" ROWS UNBOUNDED PRECEDING) AS running FROM sized) '
            "SELECT node_id,CASE WHEN running<=:budget THEN output END AS output,running>:budget AS withheld,"
            "CASE WHEN running>:budget AND tier=1 THEN jsonb_strip_nulls(jsonb_build_object('kind',output->'kind',"
            "'usage',output->'usage','droppedToolCalls',output->'droppedToolCalls','toolCalls',(SELECT "
            "coalesce(jsonb_agg(jsonb_build_object('id',c->'id','name',c->'name') ORDER BY n),'[]'::jsonb) "
            "FROM jsonb_array_elements(CASE WHEN jsonb_typeof(output->'toolCalls')='array' THEN output->'toolCalls' "
            "ELSE '[]'::jsonb END) WITH ORDINALITY AS listed(c,n)))) END AS shape FROM ranked",
            run=run_id,
            keys=keys,
            budget=PAGE_BYTES,
        )
        values: dict[str, JsonValue] = {}
        withheld: dict[str, JsonValue] = {}
        for row in rows:
            if row["withheld"]:
                shape = row["shape"]
                withheld[row["node_id"]] = json.loads(shape) if isinstance(shape, str) else shape
            else:
                values[row["node_id"]] = row["output"]
        return values, withheld

    async def turns(self, run_id: UUID, keys: list[str]) -> list[TurnRecord]:
        """What each turn's task recorded about its input, read in the database: only the flags, the loaded memory
        count and each tool result's error mark and code leave it, never a transcript."""
        if not keys:
            return []
        rows = await self.rows(
            "SELECT t.node_id,coalesce(t.payload->'input'->'toolsEnabled','true'::jsonb) AS tools_enabled,"
            "CASE WHEN t.payload->'input'->'messages'->0->>'role'='context' AND "
            "jsonb_typeof(t.payload->'input'->'messages'->0->'content'->'memory')='array' THEN "
            "jsonb_array_length(t.payload->'input'->'messages'->0->'content'->'memory') ELSE 0 END AS memory,"
            "(SELECT coalesce(jsonb_object_agg(m->>'callId',jsonb_build_object('is_error',m->'isError',"
            "'error',CASE WHEN jsonb_typeof(m->'content'->'error')='string' AND "
            "length(m->'content'->>'error')<=128 THEN m->'content'->'error' END,"
            "'status',CASE WHEN jsonb_typeof(m->'content'->'status')='string' AND "
            "length(m->'content'->>'status')<=128 THEN m->'content'->'status' END)),'{}'::jsonb) "
            "FROM jsonb_array_elements(t.payload->'input'->'messages') AS m "
            "WHERE m->>'role'='tool' AND jsonb_typeof(m->'callId')='string') AS results "
            "FROM task_intents t WHERE t.tenant_id=:tenant AND t.project_id=:project "
            "AND t.environment_id=:environment AND t.run_id=:run AND t.node_id=ANY(:keys)",
            run=run_id,
            keys=keys,
        )
        records = []
        for row in rows:
            results = row["results"] if isinstance(row["results"], dict) else json.loads(row["results"])
            records.append(
                TurnRecord(
                    key=row["node_id"],
                    tools_enabled=row["tools_enabled"] is not False,
                    memory=int(row["memory"]),
                    results={
                        identity: RecordedResult(
                            is_error=value.get("is_error") is True,
                            error=_text(value.get("error")),
                            status=_text(value.get("status")),
                        )
                        for identity, value in results.items()
                    },
                )
            )
        return records

    async def agents(self, run_id: UUID, keys: list[str]) -> dict[str, AgentState]:
        """The running agents among ``keys``, from the run's state."""
        if not keys:
            return {}
        rows = await self.rows(
            "SELECT k.key,r.state->'agents'->k.key AS agent FROM runs r,unnest(cast(:keys AS text[])) AS k(key) "
            "WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment AND r.id=:run "
            "AND r.state->'agents' ? k.key",
            run=run_id,
            keys=keys,
        )
        return {row["key"]: AgentState.model_validate_json(json.dumps(row["agent"])) for row in rows}


@service
class AgentLogService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def read(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        key: str,
        *,
        limit: int,
        cursor: str | None,
        context: AuditContext,
    ) -> AgentLogPage:
        """A page of the agent log at ``key``: a step ID pages its activations, an activation key reads that one."""
        agent_log_key(key)
        if type(limit) is not int or not 1 <= limit <= MAX_AGENT_LOG_PAGE:
            raise ValueError("An agent log page holds 1 to 20 activations")
        self.precheck(actor, scope, context)
        collection = AgentLogQuery().cursor_collection(run_id, key)
        position = decode_cursor_v2(cursor, scope, collection)
        after = _after(position)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "run.read", context=context)
            repository = AgentLogRepository(tx)
            ir = await repository.workflow(run_id)
            step = node_of(key)
            if not any(isinstance(node, AgentNode) and node.id == step for node in ir.graph.nodes):
                raise CatalogError(404, "WV-NOT-FOUND", NOT_FOUND)
            if key == step:
                keys = await repository.activations(run_id, step, after, limit + 1)
                steps = await repository.steps(run_id, step, keys[:limit]) if keys else []
            else:
                steps = await repository.steps(run_id, step, [key])
                if not any(record.key == key for record in steps):
                    raise CatalogError(404, "WV-NOT-FOUND", NOT_FOUND)
                keys = [key] if after is None or iteration(key) > after else []
            visible = keys[:limit]
            outputs, withheld = await repository.outputs(
                run_id, [record.key for record in steps if record.key not in visible]
            )
            turns = [
                record.key
                for record in steps
                if (parsed := parse_agent_key(record.key)) is not None and parsed.call is None
            ]
            intents = await repository.turns(run_id, turns)
            states = await repository.agents(run_id, visible)
            items = [
                assemble(
                    ir,
                    name,
                    steps=steps,
                    outputs=outputs,
                    intents=intents,
                    state=states.get(name),
                    withheld=withheld,
                )
                for name in visible
            ]
            next_cursor = None
            if len(keys) > limit:
                last = visible[-1]
                next_cursor = encode_cursor_v2(scope, collection, cast(JsonValue, iteration(last)), last)
            return AgentLogPage(items=items, next_cursor=next_cursor)

    def precheck(self, actor: Principal, scope: Scope, context: AuditContext) -> None:
        """`run.read` on the token principal, before any read unit opens."""
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")
        self.authorization.require(actor, scope, "run.read", context=context)


def _after(position: tuple[JsonValue, str] | None) -> list[int] | None:
    """The loop indexes a cursor continues after; anything else is not this collection's cursor."""
    if position is None:
        return None
    indexes, last = position
    if (
        not isinstance(indexes, list)
        or any(type(index) is not int or not 0 <= index <= MAX_SAFE_INTEGER for index in indexes)
        or iteration(agent_log_key(last)) != indexes
    ):
        raise ValueError("Invalid scope-bound cursor")
    return cast(list[int], indexes)
