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

"""Creating a draft test activation: who may, what it may change, and what it writes."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from transaction_platform import build, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.step_tests import DraftTestActivationRequest
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()
NOTIFY = {
    "id": "notify",
    "kind": "action",
    "uses": "notify.send-message@1.0.0",
    "with": {"object": {"text": {"ref": "/input/moveId"}}},
}
SUBMIT = {
    "id": "submit",
    "kind": "action",
    "uses": "orders.submit-order@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
}
MOVE = {"moveId": "M-1", "quantity": 2}
WRITTEN = (
    "SELECT (SELECT count(*) FROM test_activation_details WHERE source='draft') AS details,"
    "(SELECT count(*) FROM activation_revisions WHERE test) AS activations,"
    "(SELECT count(*) FROM definition_versions WHERE test) AS versions"
)


@pytest.fixture
async def drafts(worker_setup, services, access_db, provisioned, monkeypatch):
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.step_tests.test_activations import TestActivationService

    platform = await build(worker_setup, services, access_db, monkeypatch)
    sessions, owner, access, _ = access_db
    admin, _ = provisioned
    scope = platform.scope
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_WEBHOOK", "webhook-canary")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "webhook", "env", "WEAVE_CONNECTION_SECRET_WEBHOOK"),)
    )
    graph = services(sessions, secrets=secrets)

    async def person(*roles: str):
        identifier = await access.create_principal(admin, "human")
        for role in roles:
            at = scope.model_copy(update={"environment_id": None}) if role == "developer" else scope
            await access.grant(admin, identifier, Grant(role=role, scope=at))
        return await access.load_principal(identifier)

    async def execute(sql: str, **values: Any) -> None:
        async with owner() as session, session.begin():
            await session.execute(text(sql), values)

    async def query(sql: str, **values: Any) -> list[dict[str, Any]]:
        async with owner() as session:
            return [dict(row) for row in (await session.execute(text(sql), values)).mappings()]

    async def save(document: dict[str, Any], *, draft=None, revision: int = 1):
        draft = draft or uuid4()
        await execute(
            "INSERT INTO draft_revisions(id,tenant_id,project_id,revision,document) "
            "VALUES(:id,:tenant,:project,:revision,cast(:document AS jsonb))",
            id=draft,
            tenant=scope.tenant_id,
            project=scope.project_id,
            revision=revision,
            document=json.dumps(document),
        )
        return draft

    async def policy(calls: str) -> None:
        await execute(
            "INSERT INTO environment_test_policies VALUES(:tenant,:project,:environment,:calls,1,now(),:admin) "
            "ON CONFLICT(tenant_id,project_id,environment_id) DO UPDATE SET test_calls=:calls",
            tenant=scope.tenant_id,
            project=scope.project_id,
            environment=scope.environment_id,
            calls=calls,
            admin=admin.id,
        )

    def request(document: dict[str, Any], draft, **changes: Any) -> DraftTestActivationRequest:
        values: dict[str, Any] = {
            "draft_id": str(draft),
            "draft_revision": 1,
            "bindings": {"worker_release_ids": {task: str(platform.release) for task in platform.needs(document)}},
        }
        values.update(changes)
        return DraftTestActivationRequest.model_validate_json(json.dumps(values))

    service = graph.resolve(TestActivationService)

    async def create(actor, document: dict[str, Any], **changes: Any):
        draft = await save(document)
        return await service.create(actor, scope, request(document, draft, **changes), context=CONTEXT)

    await policy("enabled")
    # A step tester who may read drafts and listen for test events, but not start runs.
    tester = await person("step_tester", "developer", "deployer", "viewer")
    return SimpleNamespace(
        platform=platform,
        service=service,
        graph=graph,
        scope=scope,
        tester=tester,
        person=person,
        execute=execute,
        query=query,
        save=save,
        policy=policy,
        request=request,
        create=create,
    )


async def refused(call) -> CatalogError:
    with pytest.raises(CatalogError) as raised:
        await call
    return raised.value


async def nothing_written(env) -> bool:
    [row] = await env.query(WRITTEN)
    return (row["details"], row["activations"], row["versions"]) == (0, 0, 0)


async def test_a_draft_that_changes_nothing_gets_a_hidden_test_activation(drafts):
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    assert created.id == created.activation_id and created.side_effect == "idempotent"
    [details] = await env.query(
        "SELECT principal_id,source,step_id,side_effect,created_at,expires_at,retired_at FROM test_activation_details"
    )
    assert (details["principal_id"], details["source"], details["step_id"], details["retired_at"]) == (
        env.tester.id,
        "draft",
        None,
        None,
    )
    assert int((details["expires_at"] - details["created_at"]).total_seconds()) == 900
    [activation] = await env.query(
        "SELECT a.test,v.test AS version_test,v.name,v.version FROM activation_revisions a JOIN definition_versions v "
        "ON v.id=a.version_id WHERE a.id=:id",
        id=created.id,
    )
    assert activation == {
        "test": True,
        "version_test": True,
        "name": "notify-moves",
        "version": "1.0.0+test." + created.id.hex[:12],
    }
    # No list or lookup finds it: activations, versions, or a call pin.
    listed = await env.platform.definitions.list(env.tester, env.scope, "activations", limit=100, context=CONTEXT)
    assert str(created.id) not in json.dumps(listed, default=str)
    from firefly_weave.definitions.calls import pinned_callee
    from firefly_weave.definitions.repository import DefinitionRepository

    async with env.platform.runtime.definitions.transaction(env.scope, None, mutation=False) as tx:
        assert await pinned_callee(DefinitionRepository(tx), env.scope, created.id) is None
    [audit] = await env.query("SELECT target,event FROM access_audit WHERE action='test_activation.create'")
    assert audit["target"] == str(created.id)
    assert set(audit["event"]["details"]) == {
        "draft_id",
        "draft_revision",
        "side_effect",
        "acknowledged_side_effect",
        "connection_revision_ids",
        "workflow_activation_ids",
        "expires_at",
    }


async def test_a_draft_that_changes_data_needs_its_effect_acknowledged(drafts):
    env = drafts
    document = workflow("submit-moves", [SUBMIT, NOTIFY])
    draft = await env.save(document)
    error = await refused(env.service.create(env.tester, env.scope, env.request(document, draft), context=CONTEXT))
    assert (error.status, error.code) == (409, "WV-STEPTEST-CONFIRM")
    assert error.result == {"side_effect": "non_idempotent", "steps": ["submit"]}
    assert await nothing_written(env)
    acknowledged = env.request(document, draft, acknowledged_side_effect="non_idempotent")
    assert (await env.service.create(env.tester, env.scope, acknowledged, context=CONTEXT)).side_effect == (
        "non_idempotent"
    )


async def test_the_gate_counts_what_a_pinned_callee_changes(drafts):
    env = drafts
    callee = await env.platform.publish(workflow("submitter", [SUBMIT], callable_=True))
    step = {"id": "call", "kind": "callWorkflow", "uses": "submitter@1.0.0", "with": {"ref": "/input"}}
    document = workflow("call-submitter", [step, NOTIFY])
    draft = await env.save(document)
    bindings = {
        "worker_release_ids": {task: str(env.platform.release) for task in env.platform.needs(document)},
        "workflow_activation_ids": {"submitter@1.0.0": str(callee.id)},
    }
    error = await refused(
        env.service.create(env.tester, env.scope, env.request(document, draft, bindings=bindings), context=CONTEXT)
    )
    assert (error.code, error.result) == ("WV-STEPTEST-CONFIRM", {"side_effect": "non_idempotent", "steps": ["call"]})


async def test_the_lifetime_is_raised_to_the_longest_step_timeout(drafts):
    env = drafts
    created = await env.create(
        env.tester,
        workflow("submit-moves", [SUBMIT]),
        lifetime_seconds=10,
        acknowledged_side_effect="non_idempotent",
    )
    [details] = await env.query("SELECT created_at,expires_at FROM test_activation_details")
    # orders.submit-order may take 60 seconds.
    assert int((details["expires_at"] - details["created_at"]).total_seconds()) == 60
    assert created.expires_at == details["expires_at"]


async def test_test_calls_must_be_on_and_the_person_must_test_steps(drafts):
    env = drafts
    document = workflow("notify-moves", [NOTIFY])
    await env.policy("disabled")
    error = await refused(env.create(env.tester, document))
    assert (error.status, error.code) == (403, "WV-STEPTEST-DISABLED")
    await env.policy("enabled")
    with pytest.raises(AccessDenied):
        await env.create(await env.person("developer", "deployer", "viewer"), document)
    assert await nothing_written(env)


async def test_every_named_connection_must_be_bindable_by_the_person(drafts):
    env = drafts
    document = workflow("notify-moves", [NOTIFY])
    reader = await env.person("step_tester", "developer", "viewer")
    with pytest.raises(AccessDenied):
        await env.create(reader, document, bindings={"connection_revision_ids": {"crm": str(uuid4())}})
    assert await nothing_written(env)


async def test_a_stale_draft_revision_and_a_draft_that_doesnt_compile_are_refused(drafts):
    env = drafts
    document = workflow("notify-moves", [NOTIFY])
    draft = await env.save(document)
    await env.save(document, draft=draft, revision=2)
    error = await refused(env.service.create(env.tester, env.scope, env.request(document, draft), context=CONTEXT))
    assert (error.status, error.code) == (409, "WV-REVISION")
    broken = workflow("broken-moves", [{**NOTIFY, "uses": "notify.unknown-action@1.0.0"}])
    error = await refused(env.create(env.tester, broken))
    assert (error.status, error.code) == (422, "WV-TESTACTIVATION-COMPILE")
    diagnostics = error.result["diagnostics"]
    assert diagnostics and all(set(item) == {"code", "severity", "path", "message"} for item in diagnostics)
    assert await nothing_written(env)
    error = await refused(
        env.service.create(env.tester, env.scope, env.request(document, uuid4(), draft_revision=1), context=CONTEXT)
    )
    assert (error.status, error.code) == (404, "WV-NOT-FOUND")
