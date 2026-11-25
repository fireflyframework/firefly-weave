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

"""A turn the model can't run, on the real platform: its incident and its Agent log row name the model's own code."""

from uuid import uuid4

import pytest
import test_agent_runtime as runtime

from firefly_weave.contracts.operations import IncidentResolution
from firefly_weave.contracts.workers import TaskError
from firefly_weave.operations.agent_log import AgentLogService
from firefly_weave.runtime.recovery import RecoveryService

pytestmark = pytest.mark.integration

# The agent of the runtime tests: it asks to cancel an order, and its call waits for a distinct reviewer.
agent = runtime.agent
review_expiry = runtime.review_expiry


def stopping_desk(review: dict) -> dict:
    """The runtime tests' support desk without an On error setting: a failure stops the run at an incident."""
    document = runtime_desk(review)
    [step] = document["spec"]["steps"]
    del step["onError"]
    document["spec"]["output"] = {"ref": "/steps/support/output/result"}
    return document


runtime_desk = runtime.support_desk


async def read(setup):
    service = setup.graph.resolve(AgentLogService)
    return await service.read(
        setup.starter, setup.scope, setup.run.id, "support", limit=20, cursor=None, context=setup.context
    )


@pytest.fixture
def saved_turns(monkeypatch):
    """No saved turns; the agent fixture publishes the support desk that stops at an incident."""
    monkeypatch.setattr(runtime, "support_desk", stopping_desk)


async def failed_second_turn(setup, code: str) -> None:
    """The cancel is approved and runs; the worker then reports that the model can't run the next turn."""
    await runtime.approve(setup)
    cancel = await runtime.claim(setup)
    await runtime.complete(setup, cancel, {"status": "cancelled"})
    turn = await runtime.claim(setup)
    assert turn.capability == runtime.TURN_CAPABILITY
    async with setup.definitions.transaction(setup.scope, None) as tx:
        await setup.tasks.fail(
            tx, turn.proof, TaskError(completion_id=uuid4(), code=code, outcome="not_started"), **setup.worker
        )
    await setup.graph.resolve(RecoveryService).scan(
        setup.scope, 100, actor=setup.worker["actor"], context=setup.context
    )


@pytest.mark.parametrize("code", ["LLM_NO_TOOL_SUPPORT", "LLM_CONTEXT_LIMIT"])
async def test_the_turns_incident_and_agent_log_row_name_the_models_code(agent, code):
    await failed_second_turn(agent, code)

    # Not retried: the incident keeps the code Operate decides on, first reported as the model's code.
    incidents = await runtime.rows(agent, "SELECT node_id,code,origin_code,status FROM incidents WHERE run_id=:run")
    assert incidents == [
        {"node_id": "support#2", "code": "WV-TASK-RETRIES-EXHAUSTED", "origin_code": code, "status": "active"}
    ]
    [log] = (await read(agent)).items
    assert [None if turn.error is None else turn.error.code for turn in log.turns] == [None, code]
    assert log.error is None


async def test_only_the_turn_names_a_model_code_when_a_tool_reports_one_too(agent):
    # The cancel tool runs a plain action: a model's code its worker reports is not what its incident was first
    # reported as. Once a person reconciles the call, the next turn fails because of the model, and its incident is.
    await runtime.approve(agent)
    cancel = await runtime.claim(agent)
    async with agent.definitions.transaction(agent.scope, None) as tx:
        await agent.tasks.fail(
            tx, cancel.proof, TaskError(completion_id=uuid4(), code="LLM_POLICY", outcome="unknown"), **agent.worker
        )
    recovery = agent.graph.resolve(RecoveryService)
    await recovery.scan(agent.scope, 100, actor=agent.worker["actor"], context=agent.context)
    [held] = await runtime.rows(agent, "SELECT id,revision FROM incidents WHERE run_id=:run AND status='active'")
    async with agent.definitions.transaction(agent.scope, None) as tx:
        await agent.incidents.resolve(
            tx,
            held["id"],
            IncidentResolution(
                kind="accept_reconciled_result",
                receipt_id=uuid4(),
                output={"status": "cancelled"},
                reason="The order system shows A-17 cancelled",
                evidence_reference="ticket-17",
            ),
            held["revision"],
            **agent.worker,
        )
    turn = await runtime.claim(agent)
    assert turn.capability == runtime.TURN_CAPABILITY
    async with agent.definitions.transaction(agent.scope, None) as tx:
        await agent.tasks.fail(
            tx, turn.proof, TaskError(completion_id=uuid4(), code="LLM_POLICY", outcome="not_started"), **agent.worker
        )
    await recovery.scan(agent.scope, 100, actor=agent.worker["actor"], context=agent.context)

    incidents = await runtime.rows(
        agent, "SELECT node_id,code,origin_code,status FROM incidents WHERE run_id=:run ORDER BY node_id"
    )
    assert incidents == [
        {"node_id": "support#1.1", "code": "WV-TASK-AMBIGUOUS", "origin_code": "WV-TASK-FAILED", "status": "resolved"},
        {"node_id": "support#2", "code": "WV-TASK-RETRIES-EXHAUSTED", "origin_code": "LLM_POLICY", "status": "active"},
    ]
