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

"""Stopping a platform-event trigger at the revision the caller read: a stop that names an older revision changes
nothing, and the route passes If-Match to the service and answers the stopped trigger's revision as its ETag."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant, Principal
from firefly_weave.api.event_triggers import EventTriggerController
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.event_triggers import EventTrigger
from firefly_weave.definitions.models import CatalogError
from firefly_weave.triggers.event_triggers import EventTriggerService, trigger_view

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
PERSON = Principal(id=uuid4(), kind="human", grants=(Grant(role="deployer", scope=SCOPE),))
BASE = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"


def stored(revision: int, *, disabled: bool = False) -> dict:
    """An event_triggers row as the service selects it."""
    return {
        "id": uuid4(),
        "name": "notify",
        "activation_id": uuid4(),
        "workflow_name": "notify-team",
        "events": ["transaction.in_doubt"],
        "source_workflows": None,
        "may_reconcile": False,
        "principal_id": PERSON.id,
        "revision": revision,
        "disabled": disabled,
        "created_at": datetime(2026, 11, 22, 9, 0, tzinfo=UTC),
    }


class Database:
    """One trigger row behind the service's repository, and every statement and audit record a stop sends."""

    def __init__(self, row: dict) -> None:
        self.row = dict(row)
        self.statements: list[str] = []
        self.audits: list[str] = []

    def kind(self, sql: str) -> str:
        if "weave_control_begin" in sql:
            return "reserve"
        if sql.startswith("UPDATE event_triggers"):
            return "update"
        assert sql.startswith("SELECT") and sql.endswith("FOR UPDATE"), sql
        return "lock"

    async def rows(self, sql: str, **params) -> list[dict]:
        assert params["id"] == self.row["id"]
        kind = self.kind(sql)
        self.statements.append(kind)
        if kind == "update":
            self.row |= {"disabled": True, "revision": self.row["revision"] + 1}
        return [dict(self.row)]

    async def execute(self, sql: str, **params) -> None:
        assert params["id"] == self.row["id"]
        self.statements.append(self.kind(sql))


def service(monkeypatch, database: Database, *capabilities: str) -> EventTriggerService:
    held = set(capabilities or ("trigger.manage",))

    class Definitions:
        @asynccontextmanager
        async def transaction(self, scope, supplied, *, mutation=True):
            assert (scope, supplied, mutation) == (SCOPE, None, True)
            yield SimpleNamespace(session=database)

    def require(actor, scope, capability, context):
        if capability not in held:
            raise AccessDenied()

    async def load_principal(identifier, *, tx):
        return PERSON

    async def audit(session, actor, action, target, **details):
        database.audits.append(action)

    monkeypatch.setattr("firefly_weave.triggers.event_triggers.RuntimeRepository", lambda tx: database)
    monkeypatch.setattr("firefly_weave.triggers.event_triggers.audit", audit)
    runtime = SimpleNamespace(definitions=Definitions(), require=require)
    return EventTriggerService(runtime, SimpleNamespace(load_principal=load_principal))


@pytest.mark.parametrize("named", [2, 4])
async def test_a_stop_that_names_another_revision_is_refused_and_changes_nothing(monkeypatch, named):
    database = Database(stored(revision=3))
    with pytest.raises(CatalogError) as refused:
        await service(monkeypatch, database).disable(PERSON, SCOPE, database.row["id"], named, context=AuditContext())
    assert (refused.value.status, refused.value.code) == (409, "WV-REVISION")
    assert refused.value.message == "The trigger changed; read it again before stopping it"
    # Only the row lock: no reserve spent, no update, no audit record.
    assert (database.statements, database.audits) == (["lock"], [])
    assert (database.row["revision"], database.row["disabled"]) == (3, False)


@pytest.mark.parametrize("named", [3, None])
async def test_a_stop_that_names_the_current_revision_or_none_stops_the_trigger(monkeypatch, named):
    database = Database(stored(revision=3))
    stopped = await service(monkeypatch, database).disable(
        PERSON, SCOPE, database.row["id"], named, context=AuditContext()
    )
    assert database.statements == ["lock", "reserve", "update"]
    assert database.audits == ["event_trigger.disable"]
    assert (stopped.id, stopped.revision, stopped.disabled) == (database.row["id"], 4, True)


async def test_a_stopped_trigger_answers_as_it_is_whatever_revision_the_stop_names(monkeypatch):
    database = Database(stored(revision=5, disabled=True))
    answer = await service(monkeypatch, database).disable(PERSON, SCOPE, database.row["id"], 2, context=AuditContext())
    assert (answer.revision, answer.disabled) == (5, True)
    assert (database.statements, database.audits) == (["lock"], [])


async def test_a_person_who_may_not_manage_triggers_learns_nothing_of_the_revision(monkeypatch):
    database = Database(stored(revision=3))
    with pytest.raises(AccessDenied):
        await service(monkeypatch, database, "run.read").disable(
            PERSON, SCOPE, database.row["id"], 2, context=AuditContext()
        )
    assert database.statements == []


def request(identifier, headers: dict[str, str]) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    path = f"{BASE}/event-triggers/{identifier}/disable"
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "query_string": b"",
            "path_params": {
                "tenant": str(SCOPE.tenant_id),
                "project": str(SCOPE.project_id),
                "environment": str(SCOPE.environment_id),
                "identifier": str(identifier),
            },
            "state": {"principal": PERSON, "audit_context": AuditContext()},
        },
        receive,
    )


def recording(answer: EventTrigger):
    seen = []

    async def disable(actor, scope, identifier, expected=None, *, context):
        seen.append((actor, scope, identifier, expected))
        return answer

    return SimpleNamespace(disable=disable), seen


@pytest.mark.parametrize(("header", "expected"), [('"3"', 3), ("3", 3), (None, None)])
async def test_the_stop_route_passes_if_match_and_answers_the_stopped_revision(header, expected):
    identifier = uuid4()
    stopped = trigger_view({**stored(revision=4, disabled=True), "id": identifier})
    fake, seen = recording(stopped)
    answer = await EventTriggerController(fake).disable(request(identifier, {"If-Match": header} if header else {}))
    assert (answer.status_code, answer.headers["ETag"]) == (200, '"4"')
    assert json.loads(answer.body) == stopped.model_dump(mode="json")
    assert seen == [(PERSON, SCOPE, identifier, expected)]


@pytest.mark.parametrize("header", ['"0"', 'W/"3"', "abc", "-1", "*"])
async def test_a_malformed_if_match_is_refused_before_the_service(header):
    fake, seen = recording(trigger_view(stored(revision=1)))
    with pytest.raises(CatalogError) as refused:
        await EventTriggerController(fake).disable(request(uuid4(), {"If-Match": header}))
    assert (refused.value.status, refused.value.code) == (422, "WV-ETAG")
    assert seen == []
