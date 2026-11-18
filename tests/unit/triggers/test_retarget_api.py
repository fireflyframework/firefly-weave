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

