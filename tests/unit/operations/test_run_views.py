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

"""Scoped operational reads preserve current authority and classified metadata."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.api.run_views import RunViewController
from firefly_weave.api.transport import encode_cursor_v2
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import RunLogQuery, RunStepQuery, RunSummaryQuery
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.run_views import RunViewService

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
RUN = uuid4()
CONTEXT = AuditContext()


class ReadUnit:
    def __init__(self) -> None:
        self.opened = 0
        self.inside = False
        self.tx = SimpleNamespace(session=object(), scope=SCOPE)
        self.reads = []

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert scope == SCOPE and mutation is False
        self.opened += 1
        self.inside = True
        try:
            yield self.tx
        finally:
            self.inside = False


def principal(role: str | None = "viewer", *, active: bool = True, scope: Scope = SCOPE) -> Principal:
    grants = () if role is None else (Grant(role=role, scope=scope),)
    return Principal(id=uuid4(), kind="human", active=active, grants=grants)


def service_with(monkeypatch, current: Principal | None) -> tuple[RunViewService, ReadUnit]:
    unit = ReadUnit()

    async def load(session, identifier):
        assert unit.inside and session is unit.tx.session
        assert current is not None
        return current.model_copy(update={"id": identifier})

    monkeypatch.setattr("firefly_weave.operations.run_views.load_principal", load)
    return RunViewService(unit, AuthorizationService()), unit


def request(path: str, query: str, actor: Principal, *, identifier: bool = True) -> Request:
    params = {
        "tenant": str(SCOPE.tenant_id),
        "project": str(SCOPE.project_id),
        "environment": str(SCOPE.environment_id),
    }
    if identifier:
        params["identifier"] = str(RUN)
    value = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query.encode(),
            "headers": [],
            "path_params": params,
        }
    )
    value.state.principal = actor
    value.state.audit_context = CONTEXT
    return value


async def call(service: RunViewService, actor: Principal, operation: str):
    if operation == "run_summaries.list":
        return await service.summaries(actor, SCOPE, RunSummaryQuery(), None, context=CONTEXT)
    if operation == "runs.steps":
        return await service.steps(actor, SCOPE, RUN, RunStepQuery(), None, context=CONTEXT)
    return await service.logs(actor, SCOPE, RUN, RunLogQuery(), None, context=CONTEXT)


OPERATIONS = ("run_summaries.list", "runs.steps", "runs.logs")


@pytest.mark.parametrize("operation", ["runs.logs"])
@pytest.mark.parametrize("role", ["viewer", "execution_manager"])
async def test_run_readers_reach_the_not_served_answer(monkeypatch, operation, role):
    actor = principal(role)
    service, unit = service_with(monkeypatch, actor)
    with pytest.raises(CatalogError) as failure:
        await call(service, actor, operation)
    assert (failure.value.status, failure.value.code) == (501, "WV-UNAVAILABLE")
    assert operation in failure.value.message
    assert unit.opened == 1


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "token,current,opened",
    [
        (principal(None), principal("viewer"), 0),
        (principal("operator"), principal("operator"), 0),
        (principal("viewer", scope=Scope(tenant_id=SCOPE.tenant_id, project_id=uuid4())), principal("viewer"), 0),
        (principal("viewer"), principal("viewer", active=False), 1),
        (principal("viewer"), principal(None), 1),
    ],
)
async def test_token_and_current_principal_both_need_run_read(monkeypatch, operation, token, current, opened):
    service, unit = service_with(monkeypatch, current)
    with pytest.raises(AccessDenied):
        await call(service, token, operation)
    # A token without run.read is refused before any read unit opens; the stored principal is checked inside one.
    assert unit.opened == opened


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_each_authority_is_checked_and_audited_once(monkeypatch, caplog, operation):
    import logging

    actor = principal("viewer")
    service, unit = service_with(monkeypatch, actor)
    with caplog.at_level(logging.INFO, logger="weave.authorization"):
        try:
            await call(service, actor, operation)
        except CatalogError as error:
            assert (operation, error.status) == ("runs.logs", 501)
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "weave.authorization"]
    # One decision for the token principal and one for the principal stored now.
    assert [(record["capability"], record["outcome"]) for record in records] == [("run.read", "allow")] * 2
    assert unit.opened == 1


def test_the_current_principal_check_cannot_be_called_without_the_token_check():
    # Only the read operations and the token check are public; each read runs both checks in order.
    assert {name for name in vars(RunViewService) if not name.startswith("_")} == {
        "summaries",
        "steps",
        "logs",
        "precheck",
    }


async def test_environment_scope_is_required(monkeypatch):
    service, unit = service_with(monkeypatch, principal("viewer"))
    project = Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id)
    with pytest.raises(CatalogError) as failure:
        await service.summaries(principal("viewer"), project, RunSummaryQuery(), None, context=CONTEXT)
    assert failure.value.code == "WV-SCOPE" and unit.opened == 0


@pytest.mark.parametrize(
    "method,path,query",
    [
        ("summaries", "/run-summaries", "workflow=invoice-approval&include_test=true&order=started_desc&limit=50"),
        ("steps", f"/runs/{RUN}/steps", "limit=500"),
        ("logs", f"/runs/{RUN}/logs", "level=warning&node_id=send"),
    ],
)
async def test_controller_validates_then_delegates(monkeypatch, method, path, query):
    actor = principal("viewer")
    service, _ = service_with(monkeypatch, actor)
    controller = RunViewController(service)
    if method == "logs":
        with pytest.raises(CatalogError) as failure:
            await controller.logs(request(path, query, actor))
        assert failure.value.status == 501
    else:
        response = await getattr(controller, method)(request(path, query, actor, identifier=method != "summaries"))
        assert response.status_code == 200


@pytest.mark.parametrize(
    "method,query,code",
    [
        ("summaries", "version=1.3.0", "WV-FILTER"),
        ("summaries", "include_test=yes", None),
        ("summaries", "limit=101", None),
        ("steps", "step=send%5B3%5D", None),
        ("steps", "limit=501", None),
        ("logs", "level=trace", None),
        ("logs", "colour=red", None),
    ],
)
async def test_controller_rejects_invalid_queries_before_authorization(monkeypatch, method, query, code):
    service, unit = service_with(monkeypatch, principal("viewer"))
    controller = RunViewController(service)
    if code is None:
        with pytest.raises(ValueError):
            await getattr(controller, method)(
                request("/", query, principal("viewer"), identifier=method != "summaries")
            )
    else:
        with pytest.raises(CatalogError) as failure:
            await getattr(controller, method)(
                request("/", query, principal("viewer"), identifier=method != "summaries")
            )
        assert (failure.value.status, failure.value.code) == (422, code)
    assert unit.opened == 0


async def test_controller_rejects_cursors_from_another_filter_or_run(monkeypatch):
    actor = principal("viewer")
    service, unit = service_with(monkeypatch, actor)
    controller = RunViewController(service)
    included = RunSummaryQuery(include_test=True).cursor_collection()
    summaries = encode_cursor_v2(SCOPE, included, "2026-10-07T12:00:00Z", str(uuid4()))
    with pytest.raises(ValueError, match="cursor"):
        await controller.summaries(request("/", f"cursor={summaries}", actor, identifier=False))
    steps = encode_cursor_v2(SCOPE, RunStepQuery().cursor_collection(uuid4()), None, "send[3]")
    with pytest.raises(ValueError, match="cursor"):
        await controller.steps(request("/", f"cursor={steps}", actor))
    accepted = encode_cursor_v2(SCOPE, RunStepQuery().cursor_collection(RUN), None, "send[3]")
    assert (await controller.steps(request("/", f"cursor={accepted}", actor))).status_code == 200
    assert unit.opened == 1


async def test_not_served_problem_reaches_clients_as_501():
    from firefly_weave.api.errors import ErrorAdvice
    from firefly_weave.operations.run_views import not_served

    response = await ErrorAdvice().catalog_error(not_served("runs.logs"))
    assert response.status_code == 501
    assert json.loads(response.body) == {
        "code": "WV-UNAVAILABLE",
        "message": "This server does not serve runs.logs yet",
    }


@pytest.fixture(autouse=True)
def empty_repository(monkeypatch):
    from firefly_weave.operations.fact_reads import FactReadRepository

    async def summaries(repository, query, position):
        return []

    async def steps(repository, run_id, query, position):
        return [], True

    monkeypatch.setattr(FactReadRepository, "summary_rows", summaries)
    monkeypatch.setattr(FactReadRepository, "step_rows", steps)


async def test_fresh_authority_and_fact_read_share_transaction(monkeypatch):
    from firefly_weave.operations.fact_reads import FactReadRepository

    actor = principal()
    service, unit = service_with(monkeypatch, actor)
    events = []

    async def load(session, identifier):
        assert unit.inside
        events.append(("principal", session))
        return actor

    async def rows(repository, query, position):
        assert unit.inside
        events.append(("facts", repository.tx.session))
        return []

    monkeypatch.setattr("firefly_weave.operations.run_views.load_principal", load)
    monkeypatch.setattr(FactReadRepository, "summary_rows", rows)
    result = await service.summaries(actor, SCOPE, RunSummaryQuery(), None, context=CONTEXT)
    assert result.items == [] and unit.opened == 1
    assert [event[0] for event in events] == ["principal", "facts"]
    assert events[0][1] is events[1][1]


@pytest.mark.parametrize(
    "node,key",
    [
        # The longest valid instance key: a long step ID, fifteen maximal loop indexes and a yield count.
        ("s" * 240, "s" * 240 + "[9007199254740991]" * 15 + "~1"),
        ("s" * 512, ""),
    ],
)
async def test_step_cursor_fits_the_longest_valid_instance_key(monkeypatch, node, key):
    from datetime import UTC, datetime

    from firefly_weave.api.transport import decode_cursor_v2
    from firefly_weave.contracts.instance_keys import split_instance
    from firefly_weave.operations.fact_positions import StepPosition, step_position
    from firefly_weave.operations.fact_reads import FactReadRepository

    assert len(key or node) == 512
    split_instance(key or node)
    at = datetime(2026, 10, 9, 23, 59, 59, 999999, tzinfo=UTC)
    row = dict(
        node_id=node,
        instance_key=key,
        kind="action",
        status="succeeded",
        scheduled_at=at,
        started_at=None,
        ended_at=None,
        attempts=1,
        worker_id=None,
        error_code=None,
        handled=None,
        child_run_id=None,
    )

    async def steps(repository, run_id, query, position):
        return [row, {**row, "node_id": "t", "instance_key": ""}], True

    monkeypatch.setattr(FactReadRepository, "step_rows", steps)
    service, _ = service_with(monkeypatch, principal())
    query = RunStepQuery(limit=1)
    page = await service.steps(principal(), SCOPE, RUN, query, None, context=CONTEXT)
    assert page.next_cursor is not None and len(page.next_cursor) <= 2048
    position = step_position(decode_cursor_v2(page.next_cursor, SCOPE, query.cursor_collection(RUN)))
    assert position == StepPosition(at, node, key)


async def test_invalid_typed_cursor_precedes_run_lookup(monkeypatch):
    service, unit = service_with(monkeypatch, principal())
    with pytest.raises(ValueError):
        await service.steps(principal(), SCOPE, RUN, RunStepQuery(), ([None, "send"], "send"), context=CONTEXT)
    assert unit.opened == 0


async def test_sdk_list_retains_old_keywords_and_encodes_repeated_status():
    import httpx

    from firefly_weave.contracts.run_views import RunListQuery
    from firefly_weave.sdk.client import WeaveClient

    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    async with WeaveClient(
        "https://platform.example", lambda: "token", SCOPE, transport=httpx.MockTransport(receive)
    ) as client:
        await client.list_runs(
            limit=12,
            business_key="invoice",
            correlation_key="thread",
            status="waiting",
            include_archived=True,
            order="id",
        )
        await client.list_runs(query=RunListQuery(status=["waiting", "failed"], include_test=True, limit=5))
    assert dict(calls[0].url.params) == {
        "limit": "12",
        "business_key": "invoice",
        "correlation_key": "thread",
        "status": "waiting",
        "include_archived": "true",
        "order": "id",
    }
    assert calls[1].url.params.get_list("status") == ["failed", "waiting"]
    assert calls[1].url.params["include_test"] == "true"
    assert calls[1].url.params["order"] == "started_desc"


@pytest.mark.parametrize(
    "keywords",
    [
        {"limit": 7},
        {"cursor": "abc"},
        {"business_key": "kept"},
        {"correlation_key": "kept"},
        {"status": "waiting"},
        {"include_archived": True},
        {"order": "id"},
        {"limit": 7, "cursor": "abc", "business_key": "kept"},
    ],
)
async def test_sdk_list_rejects_a_query_mixed_with_keyword_filters(keywords):
    import httpx

    from firefly_weave.contracts.run_views import RunListQuery
    from firefly_weave.sdk.client import WeaveClient

    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    async with WeaveClient(
        "https://platform.example", lambda: "token", SCOPE, transport=httpx.MockTransport(receive)
    ) as client:
        with pytest.raises(ValueError, match="either a query"):
            await client.list_runs(query=RunListQuery(limit=3), **keywords)
    assert calls == []


async def test_runs_route_uses_strict_filters_and_preserves_explicit_id_cursor():
    from firefly_weave.api.runs import RunController
    from firefly_weave.api.transport import encode_cursor, encode_cursor_v2
    from firefly_weave.contracts.run_views import RunListQuery
    from firefly_weave.contracts.runtime import RunListFilters
    from firefly_weave.operations.fact_positions import RunPosition

    calls = []

    class Service:
        async def list(self, actor, scope, **kwargs):
            calls.append(kwargs)
            return {"items": [], "next_cursor": None}

    controller = RunController(Service(), None)
    cursor = encode_cursor(SCOPE, RunListFilters(status="waiting").cursor_collection(), RUN)
    response = await controller.discover(
        request(
            "/api/v1/tenants/t/projects/p/environments/e/runs", f"order=id&status=waiting&cursor={cursor}", principal()
        )
    )
    assert response.status_code == 200 and calls[-1]["position"] == RUN
    query = RunListQuery(status=["failed", "waiting"])
    cursor = encode_cursor_v2(SCOPE, query.cursor_collection(), None, str(RUN))
    await controller.discover(
        request(
            "/api/v1/tenants/t/projects/p/environments/e/runs",
            f"status=waiting&status=failed&cursor={cursor}",
            principal(),
        )
    )
    assert calls[-1]["position"] == RunPosition(None, RUN)
    for value in (
        "top_level_only=true&origin=call",
        "include_archived=yes",
        "colour=red",
        "limit=101",
        "workflow=one&workflow=two",
        "order=started_desc&cursor=" + encode_cursor(SCOPE, "runs", RUN),
    ):
        with pytest.raises((ValueError, CatalogError)):
            await controller.discover(request("/api/v1/tenants/t/projects/p/environments/e/runs", value, principal()))
    assert len(calls) == 2


@pytest.mark.parametrize("count", [499, 500, 501, 100000, 100001])
async def test_historical_stream_is_bounded_and_always_closed(monkeypatch, count):
    from firefly_weave.operations import fact_reads

    monkeypatch.setattr(fact_reads, "LEGACY_SECONDS", 60)
    closed = []
    maximum_heap = []
    statements = []

    class Stream:
        def mappings(self):
            return self

        async def partitions(self, size):
            assert size == 500
            # PostgreSQL returns unsorted keys; the bounded heap alone orders the page.
            keys = list(reversed(range(count)))
            for start in range(0, count, size):
                yield [{"node_id": f"send[{index}]", "status": "completed"} for index in keys[start : start + size]]

        async def close(self):
            closed.append(True)

    class Session:
        async def stream(self, statement, params, **kwargs):
            assert kwargs["execution_options"] == {"yield_per": 500}
            statements.append(str(statement))
            return Stream()

    async def metadata(repository, run_id):
        return {"node_kinds": {"send": "action"}}

    original = fact_reads.heapq.heappush

    def pushed(heap, item):
        original(heap, item)
        maximum_heap.append(len(heap))

    monkeypatch.setattr(fact_reads.FactReadRepository, "metadata", metadata)
    monkeypatch.setattr(fact_reads.heapq, "heappush", pushed)
    repository = fact_reads.FactReadRepository(SimpleNamespace(session=Session(), scope=SCOPE))
    if count > 100000:
        with pytest.raises(CatalogError) as failure:
            await repository.historical_steps(RUN, None, None, 1)
        assert (failure.value.status, failure.value.code) == (429, "WV-RUNTIME-LIMIT")
    else:
        rows, complete = await repository.historical_steps(RUN, None, None, 1)
        assert not complete and [row["instance_key"] for row in rows] == [
            "send[0]",
            "send[10000]" if count == 100000 else "send[100]",
        ]
    assert closed == [True] and max(maximum_heap) == 2
    # Sorting in SQL would make PostgreSQL read and sort every missing key before the cap could stop it.
    assert len(statements) == 1 and "ORDER BY" not in statements[0].upper()


@pytest.mark.parametrize("cancel", [False, True])
async def test_historical_stream_timeout_and_cancellation_close_cursor(monkeypatch, cancel):
    import asyncio

    from firefly_weave.operations import fact_reads

    started = asyncio.Event()
    closed = []

    class Stream:
        def mappings(self):
            return self

        async def partitions(self, size):
            started.set()
            await asyncio.Event().wait()
            yield []

        async def close(self):
            closed.append(True)

    class Session:
        async def stream(self, *args, **kwargs):
            return Stream()

    async def metadata(*args):
        return {"node_kinds": {}}

    monkeypatch.setattr(fact_reads.FactReadRepository, "metadata", metadata)
    repository = fact_reads.FactReadRepository(SimpleNamespace(session=Session(), scope=SCOPE))
    task = asyncio.create_task(repository.historical_steps(RUN, None, None, 1))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CatalogError) as failure:
            await task
        assert (failure.value.status, failure.value.code) == (429, "WV-RUNTIME-LIMIT")
    assert closed == [True]
