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

"""Retarget routes: they pass the If-Match revision and the new activation to the service, and answer the ETag."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
MOVER = Principal(id=uuid4(), kind="human", grants=(Grant(role="deployer", scope=SCOPE),))
BASE = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"


def request(body=None, headers=None, identifier=None, path="/x"):
    payload = b"" if body is None else json.dumps(body).encode()
    params = {
        "tenant": str(SCOPE.tenant_id),
        "project": str(SCOPE.project_id),
        "environment": str(SCOPE.environment_id),
        "identifier": str(identifier or uuid4()),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": BASE + path,
        "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
        "query_string": b"",
        "path_params": params,
        "state": {"principal": MOVER, "audit_context": AuditContext()},
    }
    return Request(scope, receive)


def webhook(revision=2, activation=None):
    from firefly_weave.triggers.models import Trigger

    return Trigger(
        id=uuid4(),
        principal_id=MOVER.id,
        name="orders-in",
        kind="run",
        activation_id=activation or uuid4(),
        secret_ref="signing",
        revision=revision,
    )


def recording(answer):
    seen = []

    async def retarget(actor, scope, identifier, activation_id, expected, *, context):
        seen.append((actor, scope, identifier, activation_id, expected))
        return answer

    async def read(actor, scope, identifier, *, context):
        return answer

    return SimpleNamespace(retarget=retarget, read=read), seen


@pytest.mark.parametrize("header, expected", [('"1"', 1), ("7", 7), (None, None)])
async def test_a_webhook_retarget_passes_the_revision_and_answers_the_new_one(header, expected):
    from firefly_weave.api.triggers import TriggerController

    target, identifier = uuid4(), uuid4()
    moved = webhook(revision=(expected or 1) + 1, activation=target)
    service, seen = recording(moved)
    answer = await TriggerController(service).retarget(
        request({"activation_id": str(target)}, {"If-Match": header} if header else {}, identifier)
    )
    assert answer.status_code == 200
    assert answer.headers["ETag"] == f'"{moved.revision}"'
    assert json.loads(answer.body) == moved.model_dump(mode="json")
    assert seen == [(MOVER, SCOPE, identifier, target, expected)]


@pytest.mark.parametrize("header", ['"0"', "W/1", "abc"])
async def test_a_malformed_if_match_is_refused_before_the_service(header):
    from firefly_weave.api.triggers import TriggerController

    service, seen = recording(webhook())
    with pytest.raises(CatalogError) as refused:
        await TriggerController(service).retarget(request({"activation_id": str(uuid4())}, {"If-Match": header}))
    assert (refused.value.status, refused.value.code) == (422, "WV-ETAG")
    assert seen == []


async def test_a_webhook_read_answers_its_revision_as_the_etag():
    from firefly_weave.api.triggers import TriggerController

    service, _ = recording(webhook(revision=5))
    answer = await TriggerController(service).read(request())
    assert answer.headers["ETag"] == '"5"'


def kafka(revision=2, activation=None):
    from firefly_weave.contracts.broker import BrokerTrigger

    return BrokerTrigger(
        id=uuid4(),
        binding_id=uuid4(),
        principal_id=MOVER.id,
        name="orders",
        connection_revision_id=uuid4(),
        cluster_id="orders-cluster",
        topic="orders",
        kind="run",
        activation_id=activation or uuid4(),
        dead_letter_policy="receipt",
        revision=revision,
    )


def events(revision=2, activation=None):
    from firefly_weave.contracts.providers import ProviderSource

    return ProviderSource(
        id=uuid4(),
        scope=SCOPE,
        binding_id=uuid4(),
        principal_id=MOVER.id,
        name="inbox",
        provider="fixture",
        package="fixture",
        package_version="1.0.0",
        adapter_version="1.0.0",
        schema_digest="a" * 64,
        connection_revision_id=uuid4(),
        policy={},
        kind="run",
        activation_id=activation or uuid4(),
        revision=revision,
    )


def mail(revision=2, activation=None):
    from firefly_weave.contracts.email import EmailSourceView

    return EmailSourceView(
        id=uuid4(),
        connection_revision_id=uuid4(),
        activation_id=activation or uuid4(),
        mailbox="INBOX",
        revision=revision,
        disabled=False,
    )


def platform_events(revision=2, activation=None):
    from datetime import UTC, datetime

    from firefly_weave.contracts.event_triggers import EventTrigger

    return EventTrigger(
        id=uuid4(),
        name="notify",
        activation_id=activation or uuid4(),
        events=["transaction.in_doubt"],
        workflow_name="notify-team",
        principal_id=MOVER.id,
        revision=revision,
        created_at=datetime.now(UTC),
    )


def event_controller(service):
    from firefly_weave.api.event_triggers import EventTriggerController

    return EventTriggerController(service)


def broker_controller(service):
    from firefly_weave.api.broker import BrokerController

    return BrokerController(service, SimpleNamespace())


def provider_controller(service):
    from firefly_weave.api.providers import ProviderController

    return ProviderController(service, SimpleNamespace())


def email_controller(service):
    from firefly_weave.api.email_sources import EmailSourceController

    return EmailSourceController(service)


@pytest.mark.parametrize(
    "controller, resource",
    [
        (broker_controller, kafka),
        (provider_controller, events),
        (email_controller, mail),
        (event_controller, platform_events),
    ],
    ids=["kafka", "app events", "email", "platform events"],
)
async def test_every_source_retarget_passes_the_revision_and_answers_the_new_one(controller, resource):
    target, identifier = uuid4(), uuid4()
    moved = resource(revision=4, activation=target)
    service, seen = recording(moved)
    answer = await controller(service).retarget(
        request({"activation_id": str(target)}, {"If-Match": '"3"'}, identifier)
    )
    assert (answer.status_code, answer.headers["ETag"]) == (200, '"4"')
    assert json.loads(answer.body) == moved.model_dump(mode="json")
    assert seen == [(MOVER, SCOPE, identifier, target, 3)]
    with pytest.raises(CatalogError) as refused:
        await controller(service).retarget(request({"activation_id": str(target)}, {"If-Match": "-1"}, identifier))
    assert refused.value.code == "WV-ETAG" and len(seen) == 1


async def test_kafka_and_app_event_and_email_reads_answer_the_revision_as_the_etag():
    async def provider_read(actor, scope, identifier, *, context):
        return events(revision=6)

    async def email_read(actor, scope, identifier):
        return mail(revision=8)

    kafka_service, _ = recording(kafka(revision=5))
    assert (await broker_controller(kafka_service).read(request())).headers["ETag"] == '"5"'
    provider = provider_controller(SimpleNamespace(read=provider_read))
    assert (await provider.read(request())).headers["ETag"] == '"6"'
    assert (await email_controller(SimpleNamespace(read=email_read)).read(request())).headers["ETag"] == '"8"'
    events_service, _ = recording(platform_events(revision=3))
    assert (await event_controller(events_service).read(request())).headers["ETag"] == '"3"'


async def test_the_email_source_list_pages_with_a_scope_bound_cursor():
    from firefly_weave.api.transport import decode_cursor

    last = uuid4()
    seen = []

    async def listing(actor, scope, *, limit, cursor):
        seen.append((actor, scope, limit, cursor))
        return {"items": [mail().model_dump(mode="json")], "next_cursor": str(last)}

    scope = {
        "type": "http",
        "method": "GET",
        "path": BASE + "/email/sources",
        "headers": [],
        "query_string": b"limit=1",
        "path_params": {
            "tenant": str(SCOPE.tenant_id),
            "project": str(SCOPE.project_id),
            "environment": str(SCOPE.environment_id),
        },
        "state": {"principal": MOVER, "audit_context": AuditContext()},
    }
    answer = await email_controller(SimpleNamespace(list=listing)).discover(Request(scope))
    page = json.loads(answer.body)
    assert seen == [(MOVER, SCOPE, 1, None)]
    assert decode_cursor(page["next_cursor"], SCOPE, "email_sources") == last
