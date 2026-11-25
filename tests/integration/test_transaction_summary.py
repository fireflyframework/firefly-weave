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

"""Transactions in the environment summary and its default alert rule, on real PostgreSQL: what is in doubt and what
is being undone, who may see it, and the rule every environment gets."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_operations_summary import add_incident, owner_execute, summarize
from test_worker_fleet_facts import clock, principal_with

from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration


async def add_transaction(case, run_id, state: str, *, opened: datetime, key: str = "move") -> None:
    await owner_execute(
        case,
        "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,state,"
        "business_key,participants,revision,opened_at) VALUES(:tenant,:project,:environment,:run,:key,'move',:state,"
        "'T-1029','[]',1,:opened)",
        run=run_id,
        key=key,
        state=state,
        opened=opened,
    )


async def test_the_summary_counts_transactions_in_doubt_and_undoing(operations_case):
    case = operations_case
    now = await clock(case)
    first, second, third, finished, tested = [(await case.start(f"run-{index}")).id for index in range(5)]
    await add_transaction(case, first, "in_doubt", opened=now - timedelta(hours=2))
    await add_transaction(case, second, "in_doubt", opened=now - timedelta(hours=1))
    await add_transaction(case, third, "compensating", opened=now - timedelta(seconds=754.5))
    await add_transaction(case, third, "compensating", key="move[2]", opened=now - timedelta(seconds=10))
    await add_transaction(case, finished, "compensated", opened=now - timedelta(hours=3))
    await add_transaction(case, finished, "completed", key="move[2]", opened=now - timedelta(hours=3))
    # A test run's transactions are left out, however old.
    await add_transaction(case, tested, "in_doubt", opened=now - timedelta(days=2))
    await add_transaction(case, tested, "compensating", key="move[2]", opened=now - timedelta(days=2))
    await owner_execute(case, "UPDATE run_facts SET test=true,origin='test' WHERE run_id=:run", run=tested)
    body = await summarize(case, case.actor)
    section = body["transactions"]
    assert (section["in_doubt"], section["undoing"]) == (2, 2)
    # Counted from when the oldest transaction still undoing opened, at the summary's own clock.
    observed = datetime.fromisoformat(body["observed_at"])
    expected = (observed - (now - timedelta(seconds=754.5))).total_seconds()
    assert section["oldest_undoing_seconds"] == pytest.approx(expected, abs=0.001)


async def test_a_record_an_ended_run_left_is_not_counted(operations_case):
    case = operations_case
    now = await clock(case)
    # Only an earlier version's run ended with its record unsettled: nobody can resolve it, so it must not alert.
    for status in ("succeeded", "failed", "cancelled", "timed_out"):
        ended = (await case.start(status)).id
        await add_transaction(case, ended, "in_doubt", opened=now - timedelta(days=90))
        await add_transaction(case, ended, "compensating", key="move[2]", opened=now - timedelta(days=90))
        await owner_execute(
            case, "UPDATE run_facts SET status=:status,ended_at=:at WHERE run_id=:run", status=status, at=now, run=ended
        )
    section = (await summarize(case, case.actor))["transactions"]
    assert section == {"in_doubt": 0, "undoing": 0}
    # A run that has not ended is counted in each of its statuses, and so is one whose status is unavailable.
    for status in ("queued", "running", "waiting", "suspended"):
        live = (await case.start(status)).id
        await add_transaction(case, live, "in_doubt", opened=now)
        await owner_execute(case, "UPDATE run_facts SET status=:status WHERE run_id=:run", status=status, run=live)
    unknown = (await case.start("unknown")).id
    await add_transaction(case, unknown, "compensating", opened=now - timedelta(minutes=5))
    await owner_execute(
        case, "UPDATE run_facts SET status=NULL,classification_state='unavailable' WHERE run_id=:run", run=unknown
    )
    body = await summarize(case, case.actor)
    section = body["transactions"]
    assert (section["in_doubt"], section["undoing"]) == (4, 1)
    age = (datetime.fromisoformat(body["observed_at"]) - (now - timedelta(minutes=5))).total_seconds()
    assert section["oldest_undoing_seconds"] == pytest.approx(age, abs=0.001)


async def test_the_transactions_section_needs_run_read(operations_case):
    case = operations_case
    run = (await case.start()).id
    await add_transaction(case, run, "in_doubt", opened=await clock(case))
    status_only = await principal_with(case, Grant(role="worker_operator", scope=case.scope))
    assert (await summarize(case, status_only))["transactions"] == {"state": "forbidden"}
    reader = await principal_with(case, Grant(role="viewer", scope=case.scope))
    assert (await summarize(case, reader))["transactions"] == {"in_doubt": 1, "undoing": 0}


async def test_a_summary_without_transactions_keeps_its_bytes(operations_case):
    case = operations_case
    await case.start()
    body = await summarize(case, case.actor)
    # Zeros, never left out on a server that keeps transactions; nothing undoing has no age.
    assert body["transactions"] == {"in_doubt": 0, "undoing": 0}
    assert list(body) == [
        "observed_at",
        "api",
        "health",
        "load",
        "workers",
        "targets",
        "incidents",
        "attention",
        "runs_24h",
        "alerts",
        "transactions",
    ]


async def test_an_in_doubt_incident_comes_first_among_attention_incidents(operations_case):
    case = operations_case
    now = await clock(case)
    plain_run, doubt_run = (await case.start("plain")).id, (await case.start("doubt")).id
    plain = await add_incident(case, plain_run, "work:1", opened=now - timedelta(hours=1))
    doubt = uuid4()
    await owner_execute(
        case,
        "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
        "origin_code,code,status,revision) VALUES(:id,:tenant,:project,:environment,:run,'add#undo:1','add#undo',1,"
        "'REJECTED','WV-TXN-UNDO-STUCK','active',1)",
        id=doubt,
        run=doubt_run,
    )
    await owner_execute(
        case,
        "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,opened_at,"
        "updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT :tenant,:project,:environment,"
        ":id,'add','add#undo',:opened,:opened,workflow_name,workflow_version,false,false FROM run_facts "
        "WHERE run_id=:run",
        id=doubt,
        run=doubt_run,
        opened=now,
    )
    await add_transaction(case, doubt_run, "in_doubt", opened=now)
    body = await summarize(case, case.actor)
    incidents = [item for item in body["attention"] if item["kind"] == "incident"]
    # The newer incident leads because its transaction waits for a person; it never names the business key.
    assert [item["id"] for item in incidents] == [str(doubt), str(plain)]
    assert incidents[0]["title"] == "Transaction in doubt: WV-TXN-UNDO-STUCK at add"
    assert "T-1029" not in json.dumps(body["attention"])
    # A run whose metadata is withheld names neither the code nor the step.
    await owner_execute(
        case, "UPDATE run_facts SET classification_state='unavailable' WHERE run_id=:run", run=doubt_run
    )
    withheld = [item for item in (await summarize(case, case.actor))["attention"] if item["kind"] == "incident"]
    assert (withheld[0]["id"], withheld[0]["title"]) == (str(doubt), "Transaction in doubt")


async def test_the_default_preset_installs_once_in_an_existing_environment(operations_case):
    from firefly_weave.operations.alert_loop import install_presets

    case = operations_case
    now = await clock(case)
    # An environment whose evaluator installed the defaults before transactions had one: only its first rule is there.
    async with case.tx() as tx:
        assert await install_presets(tx, now) == 2
    await owner_execute(case, "DELETE FROM alert_preset_installs WHERE preset_id='transactions-in-doubt'")
    await owner_execute(case, "DELETE FROM alert_rules WHERE preset_id='transactions-in-doubt'")
    async with case.tx() as tx:
        # The next cycle adds only the new default rule, once.
        assert await install_presets(tx, now) == 1
    async with case.tx() as tx:
        assert await install_presets(tx, now) == 0
    async with case.owner() as session:
        rules = (
            await session.execute(
                text(
                    "SELECT name,signal,comparator,threshold,for_seconds,severity,preset_id,updated_by "
                    "FROM alert_rules WHERE tenant_id=:tenant ORDER BY name"
                ),
                {"tenant": case.scope.tenant_id},
            )
        ).mappings()
        found = [dict(row) for row in rules]
    assert [(row["name"], row["preset_id"]) for row in found] == [
        ("A platform component is degraded", "platform-degraded"),
        ("Transactions in doubt", "transactions-in-doubt"),
    ]
    doubt = found[1]
    assert (doubt["signal"], doubt["comparator"], doubt["threshold"], doubt["for_seconds"], doubt["severity"]) == (
        "transactions.in_doubt",
        "gt",
        0,
        0,
        "critical",
    )
    assert doubt["updated_by"] is None
