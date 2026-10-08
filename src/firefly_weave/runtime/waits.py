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

"""Transaction-local wait arbitration. Caller holds the scoped run lock."""

from typing import Any
from uuid import uuid4

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import SignalNode, WaitNode
from firefly_weave.runtime.kernel import deadline_order, workflow
from firefly_weave.runtime.kernel import transition_async as transition
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


async def settle(repository: RuntimeRepository, row: dict[str, Any], *, terminal_only: bool = False) -> int:
    repository.tx.session.info["weave_runtime_candidate"] = row["id"]
    from firefly_weave.runtime.admission import admission
    from firefly_weave.runtime.service import view_of

    decision = admission(row)
    if decision == "unsupported":
        # Due deadlines stay pending until a platform that runs this IR applies them.
        return 0
    if decision == "unavailable":
        return await _unavailable_timeout(repository, row)
    view = view_of(row)
    if view.state.status in TERMINAL:
        return 0
    artifact = import_artifact(row["artifact"])
    ir = workflow(artifact)
    now = await repository.now()
    deadlines = await repository.rows(
        f"SELECT * FROM run_deadlines WHERE {SCOPE} AND run_id=:run AND NOT consumed ORDER BY deadline,node_id",
        run=view.id,
    )
    # Overall timeout wins whenever observed due. Other terminal signal deadlines
    # precede continuations; duration ties use stable node identity.
    duration_nodes = {node.id for node in ir.graph.nodes if isinstance(node, WaitNode)}
    deadlines.sort(key=lambda d: deadline_order(d["node_id"], d["deadline"], duration_nodes))
    for deadline in deadlines:
        if deadline["deadline"] > now:
            continue
        # A buffered receipt strictly before this wait deadline wins. Overall timeout always stops the run.
        signal = next((n for n in ir.graph.nodes if isinstance(n, SignalNode) and n.id == deadline["node_id"]), None)
        earlier = (
            []
            if signal is None
            else await repository.rows(
                f"SELECT id FROM signal_receipts WHERE {SCOPE} AND run_id=:run AND name=:name "
                "AND NOT consumed AND accepted_at<:deadline LIMIT 1",
                run=view.id,
                name=signal.name,
                deadline=deadline["deadline"],
            )
        )
        if earlier:
            continue
        elapsed = deadline["node_id"] in duration_nodes
        if terminal_only and elapsed:
            continue
        if not elapsed:
            await repository.execute("SELECT weave_control_begin('run',:id)", id=view.id)
        await repository.execute(
            "INSERT INTO wait_wakeups VALUES(:tenant,:project,:environment,:wait,:kind,:now)",
            kind="elapsed" if elapsed else "timeout",
            wait=deadline["id"],
            now=now,
        )
        event = RuntimeEvent(
            id=uuid4(),
            type="wait_elapsed" if elapsed else "timed_out",
            timestamp=now,
            sequence=view.state.accepted_sequence + 1,
            data={
                "node_id": deadline["node_id"],
                "deadline": deadline["deadline"].isoformat(),
                "wait_id": str(deadline["id"]),
            },
        )
        from firefly_weave.runtime.capacity import RuntimeCapacityError

        try:
            result = await transition(view.state, event, artifact)
        except RuntimeCapacityError:
            if elapsed:
                raise
            from firefly_weave.runtime.terminal_capacity import persist_terminal

            await persist_terminal(repository, row, event, canonical_digest({"timeout": str(deadline["id"])}))
            return 1
        await repository.persist(
            view.model_copy(update={"state": result.state}),
            event,
            result,
            canonical_digest({"elapsed" if elapsed else "timeout": str(deadline["id"])}),
        )
        return 2 if elapsed else 1
    if terminal_only:
        return 0
    # Consume the durable winner even behind the barrier: the kernel defers its
    # continuation, while its settled deadline no longer occupies scanner pages.
    while view.state.status in {"waiting", "suspended"}:
        for node in ir.graph.nodes:
            if not isinstance(node, SignalNode) or node.id not in view.state.active:
                continue
            if any(event.data.get("node_id") == node.id for event in view.state.deferred_results):
                continue
            matches = await repository.rows(
                f"SELECT * FROM signal_receipts WHERE {SCOPE} AND run_id=:run AND name=:name AND NOT consumed "
                "AND accepted_at<:deadline ORDER BY accepted_at,id LIMIT 1",
                run=view.id,
                name=node.name,
                deadline=view.state.waits[node.id],
            )
            if matches:
                break
        else:
            break
        receipt = matches[0]
        await repository.execute(
            "INSERT INTO wait_wakeups SELECT tenant_id,project_id,environment_id,id,'signal',:now "
            f"FROM run_deadlines WHERE {SCOPE} AND run_id=:run AND node_id=:node AND NOT consumed",
            run=view.id,
            node=node.id,
            now=now,
        )
        event = RuntimeEvent(
            id=receipt["id"],
            type="signal_received",
            data={
                "node_id": node.id,
                "output": receipt["payload"],
                "accepted_at": receipt["accepted_at"].isoformat(),
                "deadline": view.state.waits[node.id].isoformat(),
            },
            timestamp=now,
            sequence=view.state.accepted_sequence + 1,
        )
        result = await transition(view.state, event, artifact)
        view = view.model_copy(update={"state": result.state})
        await repository.persist(view, event, result, receipt["request_hash"])
        await repository.execute(f"UPDATE signal_receipts SET consumed=true WHERE {SCOPE} AND id=:id", id=receipt["id"])
    return 0


async def _unavailable_timeout(repository: RuntimeRepository, row: dict[str, Any]) -> int:
    from firefly_weave.contracts.runtime import UnavailableRunAcknowledgment
    from firefly_weave.runtime.models import RunState, TerminalControl
    from firefly_weave.runtime.service import event_hash

    if row["state"]["status"] in TERMINAL:
        return 0
    due = await repository.rows(
        f"SELECT * FROM run_deadlines WHERE {SCOPE} AND run_id=:run "
        "AND NOT consumed AND deadline<=clock_timestamp() ORDER BY (node_id='@run') DESC,deadline,node_id",
        run=row["id"],
    )
    from firefly_weave.runtime.admission import terminal_signals

    signals = terminal_signals(row) or {}
    selected = None
    for candidate in due:
        if candidate["node_id"] == "@run":
            selected = candidate
            break
        name = signals.get(candidate["node_id"])
        if name is not None:
            earlier = await repository.rows(
                f"SELECT 1 FROM signal_receipts WHERE {SCOPE} AND run_id=:run AND name=:name "
                "AND accepted_at<:deadline LIMIT 1",
                run=row["id"],
                name=name,
                deadline=candidate["deadline"],
            )
            if not earlier:
                selected = candidate
                break
    if selected is None:
        return 0
    state = RunState(
        status=row["state"]["status"], accepted_sequence=row["state"]["accepted_sequence"], unavailable=True
    )
    event = RuntimeEvent(
        id=uuid4(),
        type="timed_out",
        timestamp=await repository.now(),
        sequence=state.accepted_sequence + 1,
        data={
            "node_id": selected["node_id"],
            "deadline": selected["deadline"].isoformat(),
            "wait_id": str(selected["id"]),
            "code": "WV-LEGACY-UNAVAILABLE",
        },
    )
    await repository.execute("SELECT weave_control_begin('run',:id)", id=row["id"])
    await repository.execute(
        "INSERT INTO wait_wakeups VALUES(:tenant,:project,:environment,:wait,'timeout',:now)",
        wait=selected["id"],
        now=event.timestamp,
    )
    result = await transition(state, event, None)
    view = TerminalControl(id=row["id"], scope=repository.tx.scope, state=result.state)
    await repository.persist(
        view,
        event,
        result,
        event_hash(event),
        safe_response=UnavailableRunAcknowledgment(id=view.id, status="timed_out", accepted_sequence=event.sequence),
    )
    return 1
