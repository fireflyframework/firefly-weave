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

"""The incident facts an explanation reads: the caller's own access, narrow reads, one transaction, the read's clock."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import incidents
from firefly_weave.operations.fact_reads import FactReadRepository
from firefly_weave.operations.incidents import IncidentFacts, IncidentService

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
SCOPE = Scope(
    tenant_id=UUID("00000000-0000-4000-8000-0000000000c1"),
    project_id=UUID("00000000-0000-4000-8000-0000000000c2"),
    environment_id=UUID("00000000-0000-4000-8000-0000000000c3"),
)
ACTOR_ID = UUID("00000000-0000-4000-8000-0000000000c4")
INCIDENT_ID = UUID("00000000-0000-4000-8000-0000000000c5")
RUN_ID = UUID("00000000-0000-4000-8000-0000000000c6")


def principal(*roles):
    return Principal(id=ACTOR_ID, kind="human", grants=tuple(Grant(role=role, scope=SCOPE) for role in roles))


VIEW = IncidentView.model_validate_json(
    json.dumps(
        {
            "id": str(INCIDENT_ID),
            "run_id": str(RUN_ID),
            "incident_key": "charge:1",
            "node_id": "charge",
            "generation": 1,
            "origin_code": "WV-TASK-LEASE-EXPIRED",
            "code": "WV-TASK-AMBIGUOUS",
            "status": "active",
            "revision": 1,
            "instance_key": "charge[2]",
        }
    )
)


class Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class Session:
    def __init__(self, event_types, step_kind):
        self.statements = []
        self.event_types = event_types
        self.step_kind = step_kind

    async def execute(self, statement, params):
        sql = str(statement)
        self.statements.append((sql, params))
        if "FROM run_events" in sql:
            newest_first = list(reversed(self.event_types))[: params["limit"]]
            return Result([{"type": value} for value in newest_first])
        if "FROM step_facts" in sql:
            return Result([{"kind": self.step_kind}] if self.step_kind else [])
        raise AssertionError(f"unexpected statement {sql}")

    async def scalar(self, statement, params=None):
        self.statements.append((str(statement), params))
        assert "clock_timestamp()" in str(statement)
        return NOW


class Tx:
    def __init__(self, session):
        self.scope = SCOPE
        self.session = session


class Definitions:
    def __init__(self, session):
        self.authorization = AuthorizationService()
        self.session = session
        self.opened = 0

    def require(self, actor, scope, capability, context):
        self.authorization.require(actor, scope, capability, context=context)

    @asynccontextmanager
    async def transaction(self, scope, supplied, *, mutation=True):
        assert supplied is None and mutation is False
        self.opened += 1
        yield Tx(self.session)


class Runtime:
    def __init__(self, definitions):
        self.definitions = definitions

    def require(self, actor, scope, capability, context):
        self.definitions.require(actor, scope, capability, context)


class Access:
    def __init__(self, stored):
        self.stored = stored

    async def load_principal(self, identifier, *, tx):
        assert identifier == ACTOR_ID and tx is not None
        return self.stored


def service(monkeypatch, *, token, stored=None, view=VIEW, event_types=(), step_kind="action"):
    session = Session(list(event_types), step_kind)
    definitions = Definitions(session)
    reads = []

    async def incident_details(self, ids):
        reads.append(ids)
        return [{"id": ids[0]}]

    monkeypatch.setattr(FactReadRepository, "incident_details", incident_details)
    monkeypatch.setattr(incidents, "incident_of", lambda row: view)
    return IncidentService(Runtime(definitions), Access(stored or token), None), session, definitions, reads


async def facts(svc):
    return await svc.explanation_facts(principal(), SCOPE, INCIDENT_ID, context=AuditContext())


def audits(caplog, capability):
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "weave.authorization"]
    return [record["outcome"] for record in records if record["capability"] == capability]


async def test_explanation_facts_read_the_incident_its_step_kind_events_and_clock(monkeypatch):
    svc, session, definitions, reads = service(
        monkeypatch, token=principal("operator", "viewer"), event_types=["started", "task_failed", "incident_opened"]
    )
    result = await svc.explanation_facts(principal("operator", "viewer"), SCOPE, INCIDENT_ID, context=AuditContext())
    assert result == IncidentFacts(
        view=VIEW,
        step_kind="action",
        events=("started", "task_failed", "incident_opened"),
        more_events=False,
        other_events=0,
        run_read=True,
        observed_at=NOW,
    )
    assert reads == [[INCIDENT_ID]]


async def test_both_reads_and_the_clock_share_one_transaction(monkeypatch):
    actor = principal("operator", "viewer")
    svc, session, definitions, _ = service(monkeypatch, token=actor)
    await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert definitions.opened == 1
    assert [sql for sql, _ in session.statements if "run_events" in sql or "step_facts" in sql or "clock" in sql]


async def test_explanation_facts_on_an_unavailable_run_read_nothing_more(monkeypatch):
    actor = principal("operator", "viewer")
    unavailable = UnavailableResource(id=INCIDENT_ID, omissions=[])
    svc, session, _, _ = service(monkeypatch, token=actor, view=unavailable)
    assert await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext()) == unavailable
    assert session.statements == []


async def test_a_missing_incident_is_not_found(monkeypatch):
    actor = principal("operator", "viewer")
    svc, _, _, _ = service(monkeypatch, token=actor)

    async def nothing(self, ids):
        return []

    monkeypatch.setattr(FactReadRepository, "incident_details", nothing)
    with pytest.raises(CatalogError) as raised:
        await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert raised.value.status == 404


async def test_a_caller_without_incident_read_is_refused_before_any_read(monkeypatch):
    actor = principal("viewer")
    svc, session, definitions, reads = service(monkeypatch, token=actor)
    with pytest.raises(AccessDenied):
        await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert (definitions.opened, reads, session.statements) == (0, [], [])


async def test_a_stored_principal_without_incident_read_is_refused(monkeypatch):
    svc, _, _, reads = service(monkeypatch, token=principal("operator"), stored=principal("viewer"))
    with pytest.raises(AccessDenied):
        await svc.explanation_facts(principal("operator"), SCOPE, INCIDENT_ID, context=AuditContext())
    assert reads == []


async def test_a_caller_without_run_read_gets_withheld_events_and_step_kind(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="weave.authorization")
    actor = principal("operator")
    svc, session, _, _ = service(monkeypatch, token=actor, event_types=["started"])
    result = await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert (result.step_kind, result.events, result.run_read) == ("withheld", "withheld", False)
    assert [sql for sql, _ in session.statements if "run_events" in sql or "step_facts" in sql] == []
    assert audits(caplog, "run.read") == []


async def test_a_stored_principal_without_run_read_gets_withheld_events(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="weave.authorization")
    token = principal("operator", "viewer")
    svc, _, _, _ = service(monkeypatch, token=token, stored=principal("operator"))
    result = await svc.explanation_facts(token, SCOPE, INCIDENT_ID, context=AuditContext())
    assert (result.step_kind, result.events, result.run_read) == ("withheld", "withheld", False)
    assert audits(caplog, "run.read") == []


async def test_a_granted_run_read_is_audited_as_allowed_on_both_principals(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="weave.authorization")
    actor = principal("operator", "viewer")
    svc, _, _, _ = service(monkeypatch, token=actor)
    await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert audits(caplog, "run.read") == ["allow", "allow"]


async def test_the_event_statement_reads_no_data_column_and_binds_the_scope(monkeypatch):
    actor = principal("operator", "viewer")
    svc, session, _, _ = service(monkeypatch, token=actor, event_types=["started"])
    await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    for table in ("run_events", "step_facts"):
        [(sql, params)] = [(sql, params) for sql, params in session.statements if f"FROM {table}" in sql]
        assert "data" not in sql and "evidence" not in sql
        for column in ("tenant_id=:tenant", "project_id=:project", "environment_id=:environment", "run_id=:run"):
            assert column in sql
        assert (params["tenant"], params["project"], params["environment"]) == (
            SCOPE.tenant_id,
            SCOPE.project_id,
            SCOPE.environment_id,
        )
    [(sql, _)] = [(sql, params) for sql, params in session.statements if "FROM run_events" in sql]
    assert sql.startswith("SELECT type FROM run_events")


async def test_the_event_read_uses_the_run_of_the_incident_it_read(monkeypatch):
    actor = principal("operator", "viewer")
    svc, session, _, _ = service(monkeypatch, token=actor)
    await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    runs = {params["run"] for sql, params in session.statements if "FROM run_events" in sql or "FROM step_facts" in sql}
    assert runs == {RUN_ID}
    [params] = [params for sql, params in session.statements if "FROM step_facts" in sql]
    assert (params["node"], params["key"]) == ("charge", "charge[2]")


async def test_an_unknown_event_type_is_counted_never_sent(monkeypatch):
    actor = principal("operator", "viewer")
    svc, _, _, _ = service(monkeypatch, token=actor, event_types=["started", "canary-type-7f3a"])
    result = await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert (result.events, result.other_events) == (("started",), 1)


async def test_twenty_one_stored_events_keep_the_newest_twenty_oldest_first(monkeypatch):
    actor = principal("operator", "viewer")
    stored = ["started"] + ["task_failed"] * 19 + ["incident_opened"] + ["cancelled"] * 5
    svc, _, _, _ = service(monkeypatch, token=actor, event_types=stored)
    result = await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert result.events == tuple(stored[-20:])
    assert result.more_events is True


async def test_a_run_level_incident_reads_no_step_kind(monkeypatch):
    actor = principal("operator", "viewer")
    view = VIEW.model_copy(update={"node_id": None, "instance_key": ""})
    svc, session, _, _ = service(monkeypatch, token=actor, view=view)
    result = await svc.explanation_facts(actor, SCOPE, INCIDENT_ID, context=AuditContext())
    assert result.step_kind is None
    assert [sql for sql, _ in session.statements if "FROM step_facts" in sql] == []
