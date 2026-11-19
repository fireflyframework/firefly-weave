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

"""Conversation memory at run time: the loads an agent asks for, and the exchanges its completion appends.

Each runs in the transaction that persists the transition, so a rolled-back completion appends nothing. Appends lock
their conversation rows in key-hash order, so two runs completing with one key wait for each other and both land, in
commit order. A test run never reads or changes a conversation: its loads are recorded as a test's and its appends are
never written. The key's value never reaches this module; only its hash does.
"""

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import TypeAdapter, ValidationError

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.agent_conversations import CONVERSATION_MESSAGES, ConversationMessage, messages_digest
from firefly_weave.contracts.operations import MemoryLoadFact, MemoryReceipt
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.agents import UNREADABLE_CONVERSATION as UNREADABLE
from firefly_weave.runtime.agents import append_memory
from firefly_weave.runtime.models import MemoryIntent, RuntimeEvent, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact
    from firefly_weave.contracts.runtime import RunView

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
CONVERSATION = f"{SCOPE} AND workflow=:workflow AND key_hash=:key_hash"
_MESSAGES: TypeAdapter[list[ConversationMessage]] = TypeAdapter(list[ConversationMessage])


async def record_memory(repository: RuntimeRepository, run_id: UUID, event: RuntimeEvent, result: Transition) -> None:
    """Record the transition's loads as pending, apply its appends, and drop the pending loads of a run it ends."""
    intents = [command for command in result.commands if isinstance(command, MemoryIntent)]
    loads = [intent for intent in intents if intent.op == "load"]
    appends = sorted((intent for intent in intents if intent.op == "append"), key=lambda i: (i.workflow, i.key_hash))
    test = await is_test_for_memory(repository, run_id) if intents else False
    for intent in loads:
        await repository.execute(
            "INSERT INTO agent_memory_loads(tenant_id,project_id,environment_id,id,run_id,node_id,issued_sequence,"
            "workflow,key_hash,requested_at,test) VALUES(:tenant,:project,:environment,:id,:run,:node,:sequence,"
            ":workflow,:key_hash,:at,:test)",
            id=uuid4(),
            run=run_id,
            node=intent.node_id,
            sequence=event.sequence,
            workflow=intent.workflow,
            key_hash=intent.key_hash,
            at=event.timestamp,
            test=test,
        )
    if not test:
        for intent in appends:
            await _append(repository, run_id, intent, event.timestamp)
    if result.state.status in TERMINAL:
        # Nothing waits for these loads any more; delivering them marks them so the scan no longer picks the run.
        await repository.execute(
            f"UPDATE agent_memory_loads SET dropped=true,delivered_at=:at WHERE {SCOPE} AND run_id=:run "
            "AND delivered_at IS NULL",
            run=run_id,
            at=event.timestamp,
        )


async def is_test_for_memory(repository: RuntimeRepository, run_id: UUID) -> bool:
    """Whether a run is a test run for conversation memory: its activation is a test activation, or it was started as
    a test run, as the workflows a test run calls are. Such a run never reads or changes a real conversation."""
    rows = await repository.rows(
        "SELECT a.test OR coalesce(f.test,false) AS test FROM runs r JOIN activation_revisions a "
        "ON a.tenant_id=r.tenant_id AND a.project_id=r.project_id AND a.environment_id=r.environment_id "
        "AND a.id=r.activation_id LEFT JOIN run_facts f ON f.tenant_id=r.tenant_id AND f.project_id=r.project_id "
        "AND f.environment_id=r.environment_id AND f.run_id=r.id "
        f"WHERE r.{SCOPE.replace(' AND ', ' AND r.')} AND r.id=:id",
        id=run_id,
    )
    return bool(rows and rows[0]["test"])


async def _append(repository: RuntimeRepository, run_id: UUID, intent: MemoryIntent, at: datetime) -> None:
    """Add one exchange: a new conversation is inserted whole; an existing one is locked, trimmed and updated.

    An expired conversation the sweep hasn't reached yet starts over with this exchange, so expired content is never
    loaded again.
    """
    if intent.window is None or intent.ttl_seconds is None:
        raise ValueError("An append names its window and lifetime")
    expires = at + timedelta(seconds=intent.ttl_seconds)
    key = {"workflow": intent.workflow, "key_hash": intent.key_hash}
    inserted = await repository.rows(
        "INSERT INTO agent_conversations(tenant_id,project_id,environment_id,workflow,key_hash,messages,version,"
        "last_run_id,created_at,updated_at,expires_at) VALUES(:tenant,:project,:environment,:workflow,:key_hash,"
        "cast(:messages AS jsonb),1,:run,:at,:at,:expires) ON CONFLICT DO NOTHING RETURNING version",
        **key,
        messages=_json(append_memory([], intent.messages, intent.window)),
        run=run_id,
        at=at,
        expires=expires,
    )
    if inserted:
        return
    [stored] = await repository.rows(
        f"SELECT messages,version,expires_at FROM agent_conversations WHERE {CONVERSATION} FOR UPDATE", **key
    )
    existing: list[JsonObject] = [] if stored["expires_at"] <= at else stored["messages"]
    await repository.execute(
        "UPDATE agent_conversations SET messages=cast(:messages AS jsonb),version=version+1,last_run_id=:run,"
        f"updated_at=:at,expires_at=:expires WHERE {CONVERSATION}",
        **key,
        messages=_json(append_memory(existing, intent.messages, intent.window)),
        run=run_id,
        at=at,
        expires=expires,
    )


async def deliver_memory(
    repository: RuntimeRepository, view: "RunView", artifact: "CompiledArtifact"
) -> tuple["RunView", int]:
    """Deliver the pending conversation loads of a run whose lock this transaction holds.

    Each load becomes one accepted ``memory_loaded`` event with a stable identity, in the order the loads were asked
    for. A load whose agent no longer waits for it (the run ended, the agent closed, or it asks for another
    conversation) is marked dropped and never reaches the kernel. A test run's load delivers an empty conversation
    without reading the table. A stored conversation that fails validation loads empty under ``UNREADABLE`` instead
    of failing the scan, which would stall every run of the tenant.
    """
    from firefly_weave.runtime.kernel import transition_async as transition

    rows = await repository.rows(
        f"SELECT * FROM agent_memory_loads WHERE {SCOPE} AND run_id=:run AND delivered_at IS NULL "
        "ORDER BY issued_sequence,node_id FOR UPDATE",
        run=view.id,
    )
    delivered = 0
    for row in rows:
        now = await repository.now()
        agent = view.state.agents.get(row["node_id"])
        if (
            view.state.status in TERMINAL
            or agent is None
            or agent.phase != "memory"
            or agent.conversation != row["key_hash"]
        ):
            await repository.execute(
                f"UPDATE agent_memory_loads SET dropped=true,delivered_at=:at WHERE {SCOPE} AND id=:id",
                id=row["id"],
                at=now,
            )
            continue
        data = await _loaded(repository, row, now)
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/memory/{row['id']}"),
            type="memory_loaded",
            data=data,
            timestamp=now,
            sequence=view.state.accepted_sequence + 1,
        )
        transitioned = await transition(view.state, event, artifact)
        view = view.model_copy(update={"state": transitioned.state})
        await repository.execute(
            f"UPDATE agent_memory_loads SET delivered_at=:at,version=:version,expired=:expired,test=:test "
            f"WHERE {SCOPE} AND id=:id",
            id=row["id"],
            at=now,
            version=data["version"],
            expired=data["expired"],
            test=bool(row["test"]),
        )
        await repository.persist(view, event, transitioned, canonical_digest({"memory": str(row["id"])}))
        delivered += 1
    return view, delivered


async def _loaded(repository: RuntimeRepository, row: dict[str, Any], now: datetime) -> JsonObject:
    """The ``memory_loaded`` event's data for one pending load: the conversation as stored now, or empty."""
    data: JsonObject = {"node_id": row["node_id"], "key_hash": row["key_hash"], "version": 0, "expired": False}
    if row["test"]:
        return {**data, "test": True, "messages": []}
    stored = await repository.rows(
        f"SELECT messages,version,expires_at FROM agent_conversations WHERE {CONVERSATION}",
        workflow=row["workflow"],
        key_hash=row["key_hash"],
    )
    if not stored:
        return {**data, "messages": []}
    conversation = stored[0]
    data["version"] = conversation["version"]
    if conversation["expires_at"] <= now:
        return {**data, "expired": True, "messages": []}
    try:
        messages = _MESSAGES.validate_python(conversation["messages"])
    except ValidationError:
        return {**data, "code": UNREADABLE, "messages": []}
    if len(messages) > CONVERSATION_MESSAGES:
        return {**data, "code": UNREADABLE, "messages": []}
    return {**data, "messages": [cast(JsonValue, message.model_dump(mode="json")) for message in messages]}


async def memory_evidence(
    repository: RuntimeRepository, run_id: UUID, event: RuntimeEvent, result: Transition
) -> tuple[list[MemoryLoadFact], MemoryReceipt | None]:
    """The loads a transition issued, and the receipt of the load an accepted ``memory_loaded`` event delivered."""
    issued: list[MemoryLoadFact] = []
    for command in result.commands:
        if isinstance(command, MemoryIntent) and command.op == "load":
            [row] = await repository.rows(
                f"SELECT id FROM agent_memory_loads WHERE {SCOPE} AND run_id=:run AND node_id=:node "
                "AND issued_sequence=:sequence",
                run=run_id,
                node=command.node_id,
                sequence=event.sequence,
            )
            issued.append(MemoryLoadFact(load_id=row["id"], node_id=command.node_id, key_hash=command.key_hash))
    receipt = None
    if event.type == "memory_loaded":
        # Delivery marks the load before it persists the event, under the run's lock: the newest delivered load of
        # the agent is the one this event came from.
        rows = await repository.rows(
            f"SELECT id,node_id,key_hash,version,expired,test FROM agent_memory_loads WHERE {SCOPE} AND run_id=:run "
            "AND node_id=:node AND delivered_at IS NOT NULL AND NOT dropped ORDER BY issued_sequence DESC LIMIT 1",
            run=run_id,
            node=event.data.get("node_id"),
        )
        if rows:
            row = rows[0]
            receipt = MemoryReceipt(
                load=MemoryLoadFact(load_id=row["id"], node_id=row["node_id"], key_hash=row["key_hash"]),
                version=row["version"],
                expired=row["expired"],
                test=row["test"],
                messages_digest=messages_digest(event.data.get("messages")),
            )
    return issued, receipt


def _json(value: list[JsonObject]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
