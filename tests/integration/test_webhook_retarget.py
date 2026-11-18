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

"""A webhook moves to a new version of its workflow and keeps its URL; it then runs as the person who moved it."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration
ORDER = {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}


async def publish_version(definitions, actor, scope, source, *, version, input_schema=None, name=None):
    """Publish ``source`` (a workflow definition as JSON) at another version, name or input schema."""
    from firefly_weave.access.audit import AuditContext

    document = json.loads(source)
    document["metadata"]["version"] = version
    if name is not None:
        document["metadata"]["name"] = name
    if input_schema is not None:
        document["spec"]["inputSchema"] = input_schema
    key = f"{document['metadata']['name']}@{version}"
    return await definitions.publish(
        actor, scope, "Workflow", json.dumps(document), "json", key, context=AuditContext()
    )


async def activate_version(definitions, actor, scope, like, published, *, expected=None):
    """Activate ``published`` with the releases and connections of the activation ``like``."""
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.catalog import ActivationRequest

    request = ActivationRequest(
        **{
            **like.request.model_dump(),
            "scope": scope,
            "version_id": published.id,
            "artifact_digest": published.digest,
        }
    )
    return await definitions.activate(
        actor, scope, request, f"activate:{published.id}", expected_revision=expected, context=AuditContext()
    )


async def person(access, admin, scope, *roles):
    from firefly_weave.access.models import Grant

    identifier = await access.create_principal(admin, "application")
    for role in roles:
        await access.grant(admin, identifier, Grant(role=role, scope=scope))
    return await access.load_principal(identifier)


async def marked_test(owner, activation) -> UUID:
    """A copy of ``activation`` marked test, as a step test writes one."""
    identifier = uuid4()
    async with owner() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,"
                "payload,test) SELECT :id,tenant_id,project_id,environment_id,:name,1,version_id,"
                "jsonb_set(jsonb_set(payload,'{id}',to_jsonb(cast(:text_id AS text))),'{name}',"
                "to_jsonb(cast(:name AS text))),true FROM activation_revisions WHERE id=:source"
            ),
            {
                "id": identifier,
                "text_id": str(identifier),
                "name": "test-" + identifier.hex[:12],
                "source": activation.id,
            },
        )
    return identifier


def signed(raw, event="event-1"):
    import hashlib
    import hmac
    import time

    stamp = str(int(time.time()))
    return {
        "x-weave-timestamp": stamp,
        "x-weave-event-id": event,
        "x-weave-signature": hmac.new(b"webhook-canary", stamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
    }


@pytest.fixture
async def webhooks(worker_setup, worker_runtime_fixture, access_db, provisioned, services, monkeypatch):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.triggers.models import TriggerRequest
    from firefly_weave.triggers.service import WebhookService

    _, _, _, creator, scope, first, _ = worker_setup
    sessions, owner, access, _ = access_db
    admin, _ = provisioned
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_WEBHOOK", "webhook-canary")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "webhook", "env", "WEAVE_CONNECTION_SECRET_WEBHOOK"),)
    )
    graph = services(sessions, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    source = worker_runtime_fixture["source"]
    second = await activate_version(
        definitions,
        creator,
        scope,
        first,
        await publish_version(definitions, creator, scope, source, version="1.1.0"),
        expected=1,
    )
    service = graph.resolve(WebhookService)
    trigger = await service.create(
        creator,
        scope,
        TriggerRequest(name="orders-in", kind="run", activation_id=first.id, secret_ref="webhook"),
        context=AuditContext(),
    )
    mover = await person(access, admin, scope, "deployer", "operator", "viewer")
    return SimpleNamespace(
        service=service,
        definitions=definitions,
        source=source,
        trigger=trigger,
        scope=scope,
        creator=creator,
        mover=mover,
        first=first,
        second=second,
        sessions=sessions,
        owner=owner,
        access=access,
        admin=admin,
    )


async def retarget(env, activation_id, revision, *, actor=None):
    from firefly_weave.access.audit import AuditContext

    return await env.service.retarget(
        actor or env.mover, env.scope, env.trigger.id, activation_id, revision, context=AuditContext()
    )


async def refused(call):
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises(CatalogError) as raised:
        await call
    return raised.value.status, raised.value.code


async def test_a_retarget_keeps_the_url_and_starts_the_new_version_as_the_mover(webhooks):
    env = webhooks
    from firefly_weave.access.audit import AuditContext

    assert (await env.service.read(env.creator, env.scope, env.trigger.id, context=AuditContext())).revision == 1
    moved = await retarget(env, env.second.id, 1)
    assert (moved.id, moved.revision, moved.activation_id, moved.principal_id) == (
        env.trigger.id,
        2,
        env.second.id,
        env.mover.id,
    )
    assert await env.service.read(env.creator, env.scope, env.trigger.id, context=AuditContext()) == moved
    raw = b'{"eventId":"event-1","payload":3}'
    receipt = await env.service.receive(env.trigger.id, raw, signed(raw))
    async with env.owner() as session:
        row = (
            await session.execute(
                text("SELECT activation_id,principal_id FROM runs WHERE id=:id"), {"id": receipt.run_id}
            )
        ).one()
    assert tuple(row) == (env.second.id, env.mover.id)


async def test_the_audit_names_both_activations_and_principals_only(webhooks):
    env = webhooks
    await retarget(env, env.second.id, 1)
    async with env.owner() as session:
        [(target, event)] = (
            await session.execute(text("SELECT target,event FROM access_audit WHERE action='trigger.retarget'"))
        ).all()
    assert target == str(env.trigger.id)
    assert event["principal_id"] == str(env.mover.id) and event["capability"] == "trigger.manage"
    assert event["details"] == {
        "kind": "webhook",
        "from_activation_id": str(env.first.id),
        "to_activation_id": str(env.second.id),
        "from_principal_id": str(env.creator.id),
        "to_principal_id": str(env.mover.id),
    }


async def test_a_stale_or_missing_revision_is_refused(webhooks):
    env = webhooks
    await retarget(env, env.second.id, 1)
    assert await refused(retarget(env, env.first.id, 1)) == (409, "WV-REVISION")
    assert await refused(retarget(env, env.first.id, None)) == (422, "WV-ETAG")


async def test_a_disable_moves_the_revision_and_a_stopped_webhook_stays_stopped(webhooks):
    env = webhooks
    from firefly_weave.access.audit import AuditContext

    await env.service.disable(env.creator, env.scope, env.trigger.id, context=AuditContext())
    stopped = await env.service.read(env.creator, env.scope, env.trigger.id, context=AuditContext())
    assert (stopped.disabled, stopped.revision) == (True, 2)
    # A retarget prepared before the disable no longer applies, and a fresh one doesn't revive the webhook.
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-REVISION")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-TRIGGER-DISABLED")


async def test_a_changed_input_is_refused(webhooks):
    env = webhooks
    changed = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="2.0.0", input_schema=ORDER),
        expected=2,
    )
    assert await refused(retarget(env, changed.id, 1)) == (409, "WV-TRIGGER-SCHEMA-CHANGED")


async def test_another_workflow_and_a_test_activation_are_refused(webhooks):
    env = webhooks
    other = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="1.0.0", name="other-flow"),
    )
    assert await refused(retarget(env, other.id, 1)) == (409, "WV-TRIGGER-WORKFLOW")
    assert await refused(retarget(env, await marked_test(env.owner, env.second), 1)) == (404, "WV-NOT-FOUND")
    assert await refused(retarget(env, uuid4(), 1)) == (404, "WV-NOT-FOUND")


async def test_the_mover_needs_what_creating_a_webhook_needs(webhooks):
    env = webhooks
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied

    for roles in (("deployer", "viewer"), ("operator", "viewer")):
        lacking = await person(env.access, env.admin, env.scope, *roles)
        with pytest.raises(AccessDenied):
            await retarget(env, env.second.id, 1, actor=lacking)
        # Refused before the trigger is looked up, so the refusal never says whether a trigger exists.
        with pytest.raises(AccessDenied):
            await env.service.retarget(lacking, env.scope, uuid4(), env.second.id, 1, context=AuditContext())
    assert (await retarget(env, env.second.id, 1)).revision == 2
