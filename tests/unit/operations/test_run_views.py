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

"""Run summaries, steps and logs authorize run.read twice, validate, then answer 501 until served."""

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

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert scope == SCOPE and mutation is False
        self.opened += 1
        yield SimpleNamespace(session=object())


def principal(role: str | None = "viewer", *, active: bool = True, scope: Scope = SCOPE) -> Principal:
    grants = () if role is None else (Grant(role=role, scope=scope),)
    return Principal(id=uuid4(), kind="human", active=active, grants=grants)


def service_with(monkeypatch, current: Principal | None) -> tuple[RunViewService, ReadUnit]:
    unit = ReadUnit()

    async def load(session, identifier):
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


@pytest.mark.parametrize("operation", OPERATIONS)
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
    "token,current",
    [
        (principal(None), principal("viewer")),
        (principal("operator"), principal("operator")),
        (principal("viewer", scope=Scope(tenant_id=SCOPE.tenant_id, project_id=uuid4())), principal("viewer")),
        (principal("viewer"), principal("viewer", active=False)),
        (principal("viewer"), principal(None)),
    ],
)
async def test_token_and_current_principal_both_need_run_read(monkeypatch, operation, token, current):
    service, _ = service_with(monkeypatch, current)
    with pytest.raises(AccessDenied):
        await call(service, token, operation)


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
        ("steps", f"/runs/{RUN}/steps", "include=output&limit=500"),
        ("logs", f"/runs/{RUN}/logs", "level=warning&node_id=send"),
    ],
)
async def test_controller_validates_then_delegates(monkeypatch, method, path, query):
    actor = principal("viewer")
    service, _ = service_with(monkeypatch, actor)
    controller = RunViewController(service)
    with pytest.raises(CatalogError) as failure:
        await getattr(controller, method)(request(path, query, actor, identifier=method != "summaries"))
    assert failure.value.status == 501


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
    steps = encode_cursor_v2(SCOPE, RunStepQuery().cursor_collection(uuid4()), [None, "send"], "send[3]")
    with pytest.raises(ValueError, match="cursor"):
        await controller.steps(request("/", f"cursor={steps}", actor))
    accepted = encode_cursor_v2(SCOPE, RunStepQuery().cursor_collection(RUN), [None, "send"], "send[3]")
    with pytest.raises(CatalogError):
        await controller.steps(request("/", f"cursor={accepted}", actor))
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
