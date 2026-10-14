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

"""Real PostgreSQL plans of the operational reads on 200,000 runs: the intended indexes, no payload reads and a
statement count that does not grow with the page."""

import json
import os
from pathlib import Path

import pytest
from operations_support import seed_operations_dataset
from sqlalchemy import event, text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.run_views import RunSummaryQuery
from firefly_weave.contracts.worker_queries import WorkerListQuery
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.workers.service import WorkerService

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = os.environ.get("WEAVE_OPERATIONS_EVIDENCE")
SEED = "weave-plans-1"
# Tables that hold run payloads, events or step outputs: listing metadata never reads them.
PAYLOADS = {"runs", "run_events", "step_instances", "step_outputs", "task_intents"}


def nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def summarize(plan: dict) -> list[dict]:
    return [
        {
            key: node[key]
            for key in ("Node Type", "Relation Name", "Index Name", "Actual Rows", "Actual Loops", "Index Cond")
            if key in node
        }
        for node in nodes(plan)
    ]


class Captured:
    def __init__(self, case) -> None:
        self.engine = case.sessions.kw["bind"].sync_engine
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self.capture)
        return self

    def __exit__(self, *_):
        event.remove(self.engine, "before_cursor_execute", self.capture)

    def capture(self, connection, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith(("EXPLAIN", "SELECT SET_CONFIG", "SET ", "BEGIN", "SELECT PG_")):
            self.statements.append((statement, parameters))


async def explain(case, statement: str, parameters) -> dict:
    async with case.tx() as tx:
        connection = await tx.session.connection()
        result = await connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters)
        value = result.scalar()
    return (json.loads(value) if isinstance(value, str) else value)[0]


def record(key: str, value: object) -> None:
    if not EVIDENCE:
        return
    path = Path(EVIDENCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text()) if path.is_file() else {}
    current.setdefault("plans_200k", {})[key] = value
    path.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")


@pytest.fixture
async def plans(operations_case):
    seeded = await seed_operations_dataset(
        operations_case, runs=200_000, incidents=10_000, workers=100, ready_tasks=2_000, seed=SEED
    )
    async with operations_case.owner() as session:
        ids = {
            key: await session.scalar(text(f"SELECT md5(:seed||'/{path}')::uuid"), {"seed": SEED})
            for key, path in (("first_run", "run/1"), ("retried_from", "run/177794"), ("activation", "activation/7"))
        }
    return operations_case, {**seeded, **ids}


async def test_operational_reads_use_their_indexes_and_never_read_payloads(plans, services):
    case, seeded = plans
    runs = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    expected = {
        "unfiltered": ({}, "run_facts_started"),
        "updated": ({"order": "updated_desc"}, "run_facts_updated"),
        "workflow": ({"workflow": "workflow-07"}, "run_facts_workflow"),
        # One run in a hundred waits: PostgreSQL may walk the newest-first index instead, which also never sorts.
        "status": ({"status": ["waiting"]}, ("run_facts_status", "run_facts_started")),
        "activation": ({"activation_id": seeded["activation"]}, "run_facts_activation"),
        "business": ({"business_key": "order-ñ-雪-177777"}, "run_facts_business_key"),
        "correlation": ({"correlation_key": "case-17777"}, "run_facts_correlation_key"),
        "caller": ({"caller_run_id": seeded["first_run"]}, "run_facts_caller"),
        "retry": ({"retried_from_run_id": seeded["retried_from"]}, "run_facts_retry"),
    }
    for name, (filters, index) in expected.items():
        with Captured(case) as captured:
            page = await runs.summaries(
                case.actor, case.scope, RunSummaryQuery(limit=50, **filters), None, context=AuditContext()
            )
        assert page.items, name
        facts = [item for item in captured.statements if "from run_facts f" in item[0].lower()]
        assert facts, name
        plan = await explain(case, *facts[0])
        found = summarize(plan["Plan"])
        record(
            "run_summaries." + name,
            {"execution_ms": plan["Execution Time"], "statements": len(captured.statements), "nodes": found},
        )
        indexes = {node.get("Index Name") for node in found}
        relations = {node.get("Relation Name") for node in found}
        assert indexes & ({index} if isinstance(index, str) else set(index)), (name, found)
        assert not relations & PAYLOADS, (name, relations)
        assert not any(
            node["Node Type"] == "Seq Scan" and node.get("Relation Name") == "run_facts" for node in found
        ), (name, found)
        # A sort is fine over a handful of matches, never over the environment.
        assert all(
            node.get("Actual Rows", 0) <= 1000 for node in found if node["Node Type"] in ("Sort", "Incremental Sort")
        ), (name, found)
        for statement, _ in captured.statements:
            assert not PAYLOADS & {word.strip('",()') for word in statement.lower().split()}, (name, statement)

    service = services(case.sessions).resolve(IncidentService)
    with Captured(case) as captured:
        page = await service.list_all(
            case.actor, case.scope, run_id=None, query=IncidentQuery(limit=50), position=None, context=AuditContext()
        )
    assert page["items"]
    keys = [item for item in captured.statements if "from incidents i" in item[0].lower()]
    plan = await explain(case, *keys[0])
    found = summarize(plan["Plan"])
    record("incidents.list", {"execution_ms": plan["Execution Time"], "nodes": found})
    # Newest opened first walks the opening-time index; nothing reads or sorts every incident.
    assert "incident_facts_opened" in {node.get("Index Name") for node in found}, found
    assert not any(node["Node Type"] in ("Sort", "Incremental Sort", "Seq Scan") for node in found), found
    assert not {node.get("Relation Name") for node in found} & PAYLOADS, found


@pytest.mark.parametrize("kind", ["incidents", "workers"])
async def test_one_row_and_one_hundred_row_pages_issue_the_same_statements(operations_case, services, kind):
    case = operations_case
    await seed_operations_dataset(case, runs=4_000, incidents=200, workers=150, ready_tasks=100, seed=SEED)
    graph = services(case.sessions)
    counts = {}
    for limit in (1, 100):
        with Captured(case) as captured:
            if kind == "incidents":
                page = await graph.resolve(IncidentService).list_all(
                    case.actor,
                    case.scope,
                    run_id=None,
                    query=IncidentQuery(limit=limit, status=["active", "closed"]),
                    position=None,
                    context=AuditContext(),
                )
            else:
                page = await graph.resolve(WorkerService).list(
                    case.actor,
                    case.scope,
                    query=WorkerListQuery(limit=limit, state="all"),
                    position=None,
                    context=AuditContext(),
                )
        assert len(page["items"]) == limit
        counts[limit] = len(captured.statements)
    record(kind + ".statements", counts)
    assert counts[1] == counts[100], counts
