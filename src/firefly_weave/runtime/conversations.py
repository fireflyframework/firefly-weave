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
from uuid import UUID, uuid4

from firefly_weave.contracts.values import JsonObject
from firefly_weave.runtime.agents import append_memory
from firefly_weave.runtime.models import MemoryIntent, RuntimeEvent, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
CONVERSATION = f"{SCOPE} AND workflow=:workflow AND key_hash=:key_hash"


async def record_memory(repository: RuntimeRepository, run_id: UUID, event: RuntimeEvent, result: Transition) -> None:
    """Record the transition's loads as pending, apply its appends, and drop the pending loads of a run it ends."""
    intents = [command for command in result.commands if isinstance(command, MemoryIntent)]
    loads = [intent for intent in intents if intent.op == "load"]
    appends = sorted((intent for intent in intents if intent.op == "append"), key=lambda i: (i.workflow, i.key_hash))
    test = False
    if intents:
        from firefly_weave.runtime.service import is_test_run

        test = await is_test_run(repository, run_id)
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


def _json(value: list[JsonObject]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
