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

"""The PostgreSQL connector inside a transaction: a cancel or undo of an ``idempotent-command`` step records the
step's key, so the step refuses to apply when it arrives later."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_postgresql import external_database as external_database
from test_postgresql import postgres_connector as postgres_connector
from test_postgresql import sql_action_context as sql_action_context

from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.workers import LeaseTransaction

pytestmark = pytest.mark.integration

STEP = "INSERT INTO business.customers (customer_id, status) VALUES (:customerId, :status)"
CANCEL = "DELETE FROM business.customers WHERE customer_id = :customerId"
CONFIRM = "UPDATE business.customers SET status = :status WHERE customer_id = :customerId"
BOTH = {"customerId": {"type": "string"}, "status": {"type": "string"}}
ONE = {"customerId": {"type": "string"}}
SUFFIX = {"try": "", "forward": "", "confirm": "#confirm", "cancel": "#cancel", "compensate": "#undo"}


@pytest.fixture
def requests(sql_action_context):
    """Contexts for one step and its phases, all on one connection revision, so they share the operation table's
    namespace as Weave's requests for one step do."""
    base = sql_action_context(STEP, action="idempotent-command", parameters=BOTH)

    def make(run: str, phase: str | None, *, step: str = "hold"):
        statement, parameters = {
            None: (STEP, BOTH),
            "try": (STEP, BOTH),
            "forward": (STEP, BOTH),
            "confirm": (CONFIRM, BOTH),
            "cancel": (CANCEL, ONE),
            "compensate": (CANCEL, ONE),
        }[phase]
        invocation = replace(
            base.invocation, config=base.invocation.config | {"statement": statement, "parameters": parameters}
        )
        if phase is None:
            return replace(base, operation_key=f"{run}:{step}", invocation=invocation)
        mode = "reserve" if phase in {"try", "confirm", "cancel"} else "compensate"
        transaction = LeaseTransaction(id=f"{run}:move", branch_key=f"{run}:{step}", phase=phase, mode=mode)
        return replace(
            base, operation_key=f"{run}:{step}{SUFFIX[phase]}", invocation=invocation, transaction=transaction
        )

    make.scope = str(base.invocation.connection.id)
    return make


def step_input(customer: str) -> dict:
    return {"parameters": {"customerId": customer, "status": "held"}}


def cancel_input(customer: str) -> dict:
    return {"parameters": {"customerId": customer}}


async def operations(db, scope: str, run: str) -> dict[str, tuple[str, object]]:
    rows = await db.fetch(
        "SELECT operation_key, request_hash, result::text AS result FROM weave_connector.operations "
        "WHERE scope = $1 AND operation_key LIKE $2",
        scope,
        f"{run}:%",
    )
    return {
        row["operation_key"]: (row["request_hash"], None if row["result"] is None else json.loads(row["result"]))
        for row in rows
    }


async def customer(db, customer_id: str) -> str | None:
    return await db.fetchval("SELECT status FROM business.customers WHERE customer_id = $1", customer_id)


async def test_a_cancel_of_a_step_that_never_ran_leaves_a_tombstone(postgres_connector, requests, external_database):
    run, who = str(uuid4()), uuid4().hex
    result = await postgres_connector.execute(cancel_input(who), requests(run, "cancel"))
    assert result == {"rowCount": 0, "rows": []}
    rows = await operations(external_database["db"], requests.scope, run)
    assert rows[f"{run}:hold"] == ("tombstone", None)
    assert rows[f"{run}:hold#cancel"][1] == {"rowCount": 0, "rows": []}
    assert len(rows[f"{run}:hold#cancel"][0]) == 64


async def test_a_step_that_arrives_after_its_cancel_is_refused_and_changes_nothing(
    postgres_connector, requests, external_database
):
    db, run, who = external_database["db"], str(uuid4()), uuid4().hex
    await postgres_connector.execute(cancel_input(who), requests(run, "cancel"))
    before = await operations(db, requests.scope, run)
    with pytest.raises(ConnectorFailure) as refused:
        await postgres_connector.execute(step_input(who), requests(run, "try"))
    assert (refused.value.code, refused.value.outcome, refused.value.effect) == ("SQL_ALREADY_UNDONE", "failed", "none")
    assert await customer(db, who) is None
    assert await operations(db, requests.scope, run) == before


async def test_a_cancel_after_its_step_undoes_it_and_keeps_the_steps_row(
    postgres_connector, requests, external_database
):
    db, run, who = external_database["db"], str(uuid4()), uuid4().hex
    applied = await postgres_connector.execute(step_input(who), requests(run, "try"))
    step_row = (await operations(db, requests.scope, run))[f"{run}:hold"]
    assert await customer(db, who) == "held"
    undone = await postgres_connector.execute(cancel_input(who), requests(run, "cancel"))
    assert (applied["rowCount"], undone["rowCount"]) == (1, 1)
    assert await customer(db, who) is None
    rows = await operations(db, requests.scope, run)
    assert rows[f"{run}:hold"] == step_row and step_row[0] != "tombstone"
    assert sorted(rows) == [f"{run}:hold", f"{run}:hold#cancel"]


async def test_a_repeated_cancel_replays_and_leaves_one_tombstone(postgres_connector, requests, external_database):
    db, run, who = external_database["db"], str(uuid4()), uuid4().hex
    first = await postgres_connector.execute(cancel_input(who), requests(run, "cancel"))
    again = await postgres_connector.execute(cancel_input(who), requests(run, "cancel"))
    assert first == again == {"rowCount": 0, "rows": []}
    rows = await operations(db, requests.scope, run)
    assert sorted(rows) == [f"{run}:hold", f"{run}:hold#cancel"]
    assert rows[f"{run}:hold"] == ("tombstone", None)


async def test_an_undo_of_a_step_that_never_ran_leaves_a_tombstone_too(postgres_connector, requests, external_database):
    db, run, who = external_database["db"], str(uuid4()), uuid4().hex
    await postgres_connector.execute(cancel_input(who), requests(run, "compensate", step="add"))
    assert (await operations(db, requests.scope, run))[f"{run}:add"] == ("tombstone", None)
    with pytest.raises(ConnectorFailure, match="SQL_ALREADY_UNDONE"):
        await postgres_connector.execute(step_input(who), requests(run, "forward", step="add"))
    assert await customer(db, who) is None


async def test_a_cancel_waits_for_its_step_still_committing(postgres_connector, requests, external_database):
    db, run, who = external_database["db"], str(uuid4()), uuid4().hex
    committing = db.transaction()
    await committing.start()
    try:
        await db.execute(
            "INSERT INTO weave_connector.operations(scope, operation_key, request_hash) VALUES ($1, $2, $3)",
            requests.scope,
            f"{run}:hold",
            "f" * 64,
        )
        await db.execute("INSERT INTO business.customers (customer_id, status) VALUES ($1, 'held')", who)
        cancel = asyncio.create_task(postgres_connector.execute(cancel_input(who), requests(run, "cancel")))
        holder = db.get_server_pid()
        async with asyncio.timeout(10):
            while not await db.fetchval(
                "SELECT count(*) FROM pg_locks WHERE NOT granted AND $1::int = ANY(pg_blocking_pids(pid))", holder
            ):
                assert not cancel.done()
                await asyncio.sleep(0.01)
        await db.execute(
            'UPDATE weave_connector.operations SET result = \'{"rowCount": 1, "rows": []}\' '
            "WHERE scope = $1 AND operation_key = $2",
            requests.scope,
            f"{run}:hold",
        )
        await committing.commit()
    except BaseException:
        await committing.rollback()
        raise
    undone = await asyncio.wait_for(cancel, 10)
    assert undone["rowCount"] == 1
    assert await customer(db, who) is None
    assert (await operations(db, requests.scope, run))[f"{run}:hold"][0] == "f" * 64


async def test_requests_outside_a_transaction_and_confirms_write_no_tombstone(
    postgres_connector, requests, external_database
):
    db, plain, confirmed, who = external_database["db"], str(uuid4()), str(uuid4()), uuid4().hex
    await postgres_connector.execute(step_input(who), requests(plain, None))
    assert sorted(await operations(db, requests.scope, plain)) == [f"{plain}:hold"]
    await postgres_connector.execute(step_input(who), requests(confirmed, "confirm"))
    assert sorted(await operations(db, requests.scope, confirmed)) == [f"{confirmed}:hold#confirm"]
