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

"""The agent log on the real platform: read from what PostgreSQL recorded for a reviewed, refused and answered run."""

import json

import pytest
import test_agent_runtime as runtime
from sqlalchemy import text

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.agent_log import AgentLogRepository, AgentLogService, RecordedResult, TurnRecord

pytestmark = pytest.mark.integration

# The agent of the runtime tests: it asks to cancel an order, and its call waits for a distinct reviewer.
agent = runtime.agent
review_expiry = runtime.review_expiry
saved_turns = runtime.saved_turns
approve, claim, complete, usage = runtime.approve, runtime.claim, runtime.complete, runtime.usage


def turn(*calls: tuple[str, dict]) -> dict:
    return {
        "kind": "tool_calls",
        "text": "",
        "toolCalls": [
            {"id": f"t2c{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(calls, start=1)
        ],
        "usage": usage(),
        "provider": "openai-chat",
        "model": "fixture",
        "finishReason": "tool_calls",
    }


def answer(result: str) -> dict:
    return {
        "kind": "final",
        "text": "",
        "result": result,
        "usage": {"requests": 1, "inputTokens": 120, "outputTokens": 12},
        "provider": "openai-chat",
        "model": "fixture",
        "finishReason": "stop",
    }


async def read(setup, key="support", actor=None, **page):
    service = setup.graph.resolve(AgentLogService)
    return await service.read(
        actor or setup.starter,
        setup.scope,
        setup.run.id,
        key,
        limit=page.get("limit", 20),
        cursor=page.get("cursor"),
        context=setup.context,
    )


async def finish(setup) -> None:
    """Approve the cancel, let it run, then a turn that names an unknown tool, then the answer."""
    await approve(setup)
    cancel = await claim(setup)
    assert cancel.capability == "orders.cancel@1.0.0"
    await complete(setup, cancel, {"status": "cancelled"})
    second = await claim(setup)
    await complete(setup, second, turn(("drop_table", {"name": "orders"})))
    third = await claim(setup)
    await complete(setup, third, answer("Order A-17 is cancelled."))


async def test_a_waiting_agent_shows_its_review(agent):
    [log] = (await read(agent)).items

    assert (log.status, log.stop, log.ended_at) == ("waiting", None, None)
    [first] = log.turns
    assert (first.input_tokens, first.output_tokens, first.ended_at is not None) == (90, 10, True)
    [call] = first.calls
    assert (call.instance_key, call.name, call.status) == ("support#1.1", "cancel_order", "review")
    assert (call.target.kind, call.target.reference) == ("action", "orders-cancel@1.0.0")
    assert call.arguments.available and call.arguments.value == {"id": "A-17"}


async def test_the_log_reads_what_the_run_recorded(agent):
    await finish(agent)

    page = await read(agent)

    assert page.next_cursor is None
    [log] = page.items
    assert (log.instance_key, log.status, log.stop, log.error) == ("support", "succeeded", "final", None)
    assert [turn.turn for turn in log.turns] == [1, 2, 3]
    cancel = log.turns[0].calls[0]
    assert (cancel.status, cancel.review.decision, cancel.error) == ("succeeded", "approve", None)
    assert cancel.output.available and cancel.output.value == {"status": "cancelled"}
    assert cancel.started_at is not None and cancel.ended_at >= cancel.started_at
    # The unknown tool never ran; the reason the model read comes from the next turn's recorded input.
    [refused] = log.turns[1].calls
    assert (refused.status, refused.error.code, refused.target, refused.output) == (
        "refused",
        "UNKNOWN_TOOL",
        None,
        None,
    )
    assert log.turns[2].final.value == "Order A-17 is cancelled."
    totals = log.totals
    assert (totals.turns, totals.tool_calls, totals.input_tokens, totals.output_tokens) == (3, 2, 300, 32)
    assert totals.duration_ms is not None and log.memory.loaded == 0


async def test_a_person_without_run_read_gets_403_and_no_data(agent, access_db, provisioned):
    admin, scopes = provisioned
    outsider = await access_db[2].create_principal(admin, "human")
    await access_db[2].grant(admin, outsider, Grant(role="task_participant", scope=scopes[0]))

    with pytest.raises(AccessDenied):
        await read(agent, actor=await access_db[2].load_principal(outsider))


@pytest.mark.parametrize("key", ["nope", "support[3]"])
async def test_an_unknown_key_is_404(agent, key):
    with pytest.raises(CatalogError) as failure:
        await read(agent, key)

    assert (failure.value.status, failure.value.code) == (404, "WV-NOT-FOUND")


async def test_a_steps_activations_are_listed_in_iteration_order(agent):
    """Loop indexes sort as numbers, outer loop first, and a page continues after its last activation."""
    keys = [f"support[{outer}][{inner}]" for outer in (2, 10) for inner in (9, 10)] + ["support[1]"]
    async with agent.db[1].begin() as session:
        for key in keys:
            await session.execute(
                text(
                    "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,kind,"
                    "status) VALUES(:tenant,:project,:environment,:run,'support',:key,'agent','succeeded')"
                ),
                {
                    "tenant": agent.scope.tenant_id,
                    "project": agent.scope.project_id,
                    "environment": agent.scope.environment_id,
                    "run": agent.run.id,
                    "key": key,
                },
            )

    async with agent.definitions.transaction(agent.scope, None) as tx:
        repository = AgentLogRepository(tx)
        listed = await repository.activations(agent.run.id, "support", None, 10)
        after = await repository.activations(agent.run.id, "support", [2, 10], 10)

    # The run's own activation (the step outside a loop) comes first.
    assert listed == [
        "support",
        "support[1]",
        "support[2][9]",
        "support[2][10]",
        "support[10][9]",
        "support[10][10]",
    ]
    assert after == ["support[10][9]", "support[10][10]"]


async def change(setup, sql: str, node: str, **values) -> None:
    """Rewrite what the run recorded at ``node``, as a store could hold it."""
    async with setup.db[1].begin() as session:
        await session.execute(
            text(
                sql + " WHERE tenant_id=:tenant AND project_id=:project AND environment_id=:environment "
                "AND run_id=:run AND node_id=:node"
            ),
            {
                "tenant": setup.scope.tenant_id,
                "project": setup.scope.project_id,
                "environment": setup.scope.environment_id,
                "run": setup.run.id,
                "node": node,
                **values,
            },
        )


async def test_values_past_the_page_limit_are_left_out_and_the_page_still_reads(agent):
    """One activation's values over 8 MiB: the smaller ones show, the larger ones are left out with their reason."""
    await finish(agent)
    large = "x" * (9 * 1024 * 1024)
    await change(
        agent,
        "UPDATE step_instances SET output=jsonb_build_object('status',cast(:large AS text))",
        "support#1.1",
        large=large,
    )
    await change(
        agent,
        "UPDATE step_instances SET output=jsonb_set(output,'{result}',to_jsonb(cast(:large AS text)))",
        "support#3",
        large=large,
    )

    [log] = (await read(agent, limit=1)).items

    limited = [("", "resource_limit")]
    cancel = log.turns[0].calls[0]
    assert (cancel.status, cancel.arguments.value, cancel.review.decision) == ("succeeded", {"id": "A-17"}, "approve")
    assert cancel.output.available is False and [(o.path, o.reason) for o in cancel.output.omissions] == limited
    answer = log.turns[2]
    assert answer.final.available is False and [(o.path, o.reason) for o in answer.final.omissions] == limited
    assert (answer.input_tokens, answer.output_tokens, answer.calls) == (120, 12, [])
    assert (log.turns[1].calls[0].status, log.status, log.stop) == ("refused", "succeeded", "final")
    assert large not in log.model_dump_json()


async def test_a_small_value_after_one_left_out_still_shows(agent):
    """Only the values a page keeps count against its limit: the cancel's small output, read after a 9 MiB final
    answer that is left out, still fits."""
    await finish(agent)
    large = "x" * (9 * 1024 * 1024)
    await change(
        agent,
        "UPDATE step_instances SET output=jsonb_set(output,'{result}',to_jsonb(cast(:large AS text)))",
        "support#3",
        large=large,
    )

    [log] = (await read(agent, limit=1)).items

    answer, cancel = log.turns[2], log.turns[0].calls[0]
    assert answer.final.available is False and [(o.path, o.reason) for o in answer.final.omissions] == [
        ("", "resource_limit")
    ]
    assert (answer.input_tokens, answer.output_tokens) == (120, 12)
    assert cancel.output.available and cancel.output.value == {"status": "cancelled"}
    assert log.turns[0].calls[0].arguments.value == {"id": "A-17"}


async def test_a_turns_recorded_input_gives_its_tools_flag_memory_and_result_codes(agent):
    """Read in the database: whether tools were on (off on the forced final turn), how many remembered messages the
    first message carried, and each tool result's error mark, code (128 characters at most) and status."""
    await finish(agent)
    messages = [
        {
            "role": "context",
            "content": {"memory": [{"role": "user", "content": "A-9?"}, {"role": "assistant", "content": "Shipped."}]},
        },
        {"role": "user", "content": "Cancel A-17"},
        {"role": "tool", "callId": "t1c1", "isError": True, "content": {"error": "UNKNOWN_TOOL"}},
        {"role": "tool", "callId": "t1c2", "isError": True, "content": {"error": "E" * 129}},
        {"role": "tool", "callId": "t1c3", "isError": True, "content": {"status": "expired"}},
    ]
    await change(
        agent,
        "UPDATE task_intents SET payload=jsonb_set(jsonb_set(payload,'{input,toolsEnabled}','false'),"
        "'{input,messages}',cast(:messages AS jsonb))",
        "support#3",
        messages=json.dumps(messages),
    )

    async with agent.definitions.transaction(agent.scope, None) as tx:
        records = await AgentLogRepository(tx).turns(agent.run.id, ["support#2", "support#3"])

    assert sorted(records, key=lambda record: record.key) == [
        # Turn 2 as the platform recorded it: tools on, no memory, and the cancel's result the model read.
        TurnRecord("support#2", True, 0, {"t1c1": RecordedResult(False, None, "cancelled")}),
        TurnRecord(
            "support#3",
            False,
            2,
            {
                "t1c1": RecordedResult(True, "UNKNOWN_TOOL", None),
                "t1c2": RecordedResult(True, None, None),
                "t1c3": RecordedResult(True, None, "expired"),
            },
        ),
    ]
