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
"""A Weave AI question reads its attachments as the credential that asked, and a run attachment that turns
unavailable between the read sent to the model and the read after the reply discards the reply."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import firefly_weave.operations.lumi as module
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import CredentialIdentity, Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.lumi import LumiAskRequest, LumiReply
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import RunSummary, StepFactPage
from firefly_weave.operations.lumi import LumiService
from firefly_weave.operations.lumi_operations import Explained
from firefly_weave.operations.run_views import RunViewService

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
PRINCIPAL_ID = uuid4()
RUN_ID = uuid4()
START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
IDENTITY = CredentialIdentity(provider_id="corporate", issuer="https://id.example", subject="ada", client_id="studio")
WORKFLOW = {"kind": "Workflow", "metadata": {"name": "n", "version": "1.0.0"}, "spec": {"steps": []}}


def principal(*roles, identity=None):
    grants = tuple(Grant(role=role, scope=SCOPE) for role in roles)
    return Principal(id=PRINCIPAL_ID, kind="human", grants=grants, credential_identity=identity)


def run_summary():
    return RunSummary.model_validate(
        {
            "id": RUN_ID,
            "workflow": {"name": "support-inbox", "version": "1.2.0", "definition_version_id": uuid4()},
            "activation_id": uuid4(),
            "activation": {"name": "support-inbox", "revision": 3},
            "status": "failed",
            "paused": False,
            "test": False,
            "origin": None,
            "caller": None,
            "retried_from_run_id": None,
            "started_at": START,
            "updated_at": START,
            "ended_at": START,
            "duration_ms": 0,
            "business_key": None,
            "correlation_key": None,
            "failed_step": None,
            "active_incidents": 0,
            "archived": False,
        }
    )


class Readers:
    """Each owning service's read, recording the principal it was called with."""

    def __init__(self, summaries=None):
        self.principals: list[tuple[str, Principal]] = []
        self.summaries = list(summaries) if summaries is not None else None

    async def summary(self, actor, scope, run_id, *, context):
        self.principals.append(("run", actor))
        return self.summaries.pop(0) if self.summaries is not None else run_summary()

    async def steps(self, actor, scope, run_id, query, position, *, context):
        self.principals.append(("run steps", actor))
        return StepFactPage(items=[], complete=True)

    async def draft(self, actor, scope, collection, identifier, *, context):
        self.principals.append(("draft", actor))
        return {"document": WORKFLOW, "revision": 1}

    async def inspect(self, tx, identifier, *, actor, scope, context):
        self.principals.append(("simulation", actor))
        return SimpleNamespace(view=SimpleNamespace(status="paused", current_nodes=(), diagnostics=()))

    async def deployment(self, collection, identifier, *, actor, scope, context):
        self.principals.append(("deployment-target", actor))
        return SimpleNamespace()

    async def explained(self, kind, request):
        self.principals.append((kind, request.actor))
        return Explained({"status": "open"}, frozenset({"available"}))


def asker(monkeypatch, readers, stored, *, runs=None):
    """A Weave AI service whose stored principal is ``stored``; the owning reads are ``readers``."""

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=SCOPE, session=None)

    async def load_principal(session, identifier):
        return stored

    monkeypatch.setattr(module, "load_principal", load_principal)
    monkeypatch.setattr(module, "deployment_context", lambda kind, record: {"name": "edge"})
    monkeypatch.setattr(
        module,
        "ConnectionRepository",
        lambda tx: SimpleNamespace(revision=AsyncMock(return_value=SimpleNamespace(config={}))),
    )
    definitions = SimpleNamespace(transaction=transaction, require=lambda *args: None, read=readers.draft)
    gateway = SimpleNamespace(configured=True, ask=AsyncMock(return_value=LumiReply(answer="An answer")))
    service = LumiService(
        SimpleNamespace(definitions=definitions, secrets=None),
        None,
        SimpleNamespace(inspect=readers.inspect),
        gateway,
        SimpleNamespace(read=readers.deployment),
        SimpleNamespace(current=lambda: None),
        runs or SimpleNamespace(summary=readers.summary, steps=readers.steps),
        SimpleNamespace(read=readers.explained),
    )
    service._read = AsyncMock(
        return_value=SimpleNamespace(
            enabled=True,
            revision=1,
            connection_revision_id=uuid4(),
            context_profile="auto",
            profile=SimpleNamespace(timeout_seconds=5, options=SimpleNamespace(max_tokens=512)),
        )
    )
    service._connection = lambda *args: None
    return service, gateway


def ask_request(*kinds):
    return LumiAskRequest(message="Explain", attachments=[{"kind": kind, "id": uuid4()} for kind in kinds])


@pytest.mark.parametrize(
    ("kind", "reads"),
    [
        ("run", ["run", "run steps"]),
        ("draft", ["draft"]),
        ("simulation", ["simulation"]),
        ("deployment-target", ["deployment-target"]),
        ("worker", ["worker"]),
    ],
)
async def test_each_attachment_is_read_as_the_credential_that_asked(monkeypatch, kind, reads):
    # The stored principal is the same person without the credential: an owning service handed it checks, and
    # audits, an identity that never made the request.
    token = principal("lumi_user", "viewer", identity=IDENTITY)
    readers = Readers()
    service, _ = asker(monkeypatch, readers, stored=principal("lumi_user", "viewer"))
    answer = await service.ask(token, SCOPE, ask_request(kind), context=AuditContext(), summary=None)
    assert answer.answer == "An answer"
    # Both reads, the one sent to the model and the one after the reply, are the caller's.
    assert readers.principals == [(read, token) for read in reads] * 2
    assert all(actor.credential_identity == IDENTITY for _, actor in readers.principals)


async def test_a_denied_attachment_read_audits_the_credential_that_asked(monkeypatch, caplog):
    # The run's owning service checks `run.read` on the principal it is handed before it opens a read; its denial
    # record must name the credential, not an identity-less copy loaded from storage.
    token = principal("lumi_user", identity=IDENTITY)
    runs = RunViewService(None, AuthorizationService())
    service, gateway = asker(monkeypatch, Readers(), stored=principal("lumi_user"), runs=runs)
    caplog.set_level(logging.INFO, logger="weave.authorization")
    with pytest.raises(AccessDenied):
        await service.ask(token, SCOPE, ask_request("run"), context=AuditContext(), summary=None)
    gateway.ask.assert_not_awaited()
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "weave.authorization"]
    denials = [record for record in records if record["outcome"] == "deny"]
    assert [(record["capability"], record["identity"]) for record in denials] == [("run.read", IDENTITY.model_dump())]


UNAVAILABLE = UnavailableResource(id=RUN_ID, omissions=[])


@pytest.mark.parametrize("second", [UNAVAILABLE, None], ids=["classified or blocked by policy", "its facts gone"])
async def test_a_run_that_turns_unavailable_after_the_model_read_it_discards_the_reply(monkeypatch, second):
    token = principal("lumi_user", "viewer", identity=IDENTITY)
    readers = Readers(summaries=[run_summary(), second])
    service, gateway = asker(monkeypatch, readers, stored=token)
    with pytest.raises(AccessDenied):
        await service.ask(token, SCOPE, ask_request("run"), context=AuditContext(), summary=None)
    # The model saw the run's facts; the reply built from them is never released.
    sent = gateway.ask.await_args.args[2]
    assert sent["resources"][0]["data"]["workflow"] == {"name": "support-inbox", "version": "1.2.0"}
