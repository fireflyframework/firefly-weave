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

"""Draft test activation routes: what each passes to the service, and what each answers."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.step_tests import DraftTestActivation, DraftTestListen
from firefly_weave.contracts.surface import OPERATIONS

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
TESTER = Principal(id=uuid4(), kind="human", grants=(Grant(role="step_tester", scope=SCOPE),))
BASE = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"


def request(method="POST", body=None, headers=None, identifier=None, suffix=""):
    payload = b"" if body is None else json.dumps(body).encode()
    params = {
        "tenant": str(SCOPE.tenant_id),
        "project": str(SCOPE.project_id),
        "environment": str(SCOPE.environment_id),
    }
    if identifier is not None:
        params["identifier"] = str(identifier)

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "scheme": "https",
        "server": ("weave.example.com", 443),
        "path": BASE + "/test-activations" + suffix,
        "headers": [(b"host", b"weave.example.com")]
        + [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
        "query_string": b"",
        "path_params": params,
        "state": {"principal": TESTER, "audit_context": AuditContext()},
    }
    return Request(scope, receive)


def controller(**methods):
    from firefly_weave.api.test_activations import TestActivationController

    return TestActivationController(SimpleNamespace(**methods))


async def test_create_answers_201_with_the_test_activation():
    created = DraftTestActivation(
        id=(identifier := uuid4()),
        activation_id=identifier,
        artifact_digest="a" * 64,
        side_effect="read_only",
        expires_at=datetime.now(UTC),
    )
    seen = []

    async def create(actor, scope, body, *, context):
        seen.append((actor, scope, body.draft_revision, body.lifetime_seconds))
        return created

    draft = str(uuid4())
    answer = await controller(create=create).create(request(body={"draft_id": draft, "draft_revision": 4}))
    assert answer.status_code == 201 and json.loads(answer.body) == created.model_dump(mode="json")
    assert seen == [(TESTER, SCOPE, 4, 900)]


async def test_a_run_passes_the_input_and_an_optional_idempotency_key():
    seen = []
    view = SimpleNamespace(model_dump=lambda mode: {"id": "run"})

    async def start(actor, scope, identifier, body, key, *, context):
        seen.append((identifier, body.input, key))
        return view

    identifier = uuid4()
    first = await controller(start=start).start(request(body={"input": {"moveId": "M-1"}}, identifier=identifier))
    again = await controller(start=start).start(
        request(body={"input": 3}, headers={"Idempotency-Key": "click-1"}, identifier=identifier)
    )
    assert first.status_code == again.status_code == 201
    assert seen == [(identifier, {"moveId": "M-1"}, None), (identifier, 3, "click-1")]


async def test_listen_builds_the_test_url_from_the_platform_address_the_caller_used():
    seen = []
    listened = DraftTestListen(test_url="https://weave.example.com/webhooks/x", expires_at=datetime.now(UTC))

    async def listen(actor, scope, identifier, body, base_url, *, context):
        seen.append((body.secret_ref, body.tolerance_seconds, base_url))
        return listened

    answer = await controller(listen=listen).listen(request(body={"secret_ref": "signing"}, identifier=uuid4()))
    assert answer.status_code == 201 and json.loads(answer.body) == listened.model_dump(mode="json")
    assert seen == [("signing", 300, "https://weave.example.com/")]


async def test_retire_answers_204_without_a_body():
    seen = []

    async def retire(actor, scope, identifier, *, context):
        seen.append(identifier)

    identifier = uuid4()
    answer = await controller(retire=retire).retire(request("DELETE", identifier=identifier))
    assert (answer.status_code, answer.body) == (204, b"")
    assert seen == [identifier]


def test_the_four_operations_need_step_test_and_listening_also_trigger_manage():
    expected = {
        "test_activations.create": ("POST", "/test-activations", "step.test", (201,)),
        "test_activations.run": ("POST", "/test-activations/{identifier}/runs", "step.test", (201,)),
        "test_activations.listen": (
            "POST",
            "/test-activations/{identifier}/listen",
            "step.test and trigger.manage",
            (201,),
        ),
        "test_activations.retire": ("DELETE", "/test-activations/{identifier}", "step.test", (204,)),
    }
    for identifier, (method, path, capability, statuses) in expected.items():
        operation = OPERATIONS[identifier]
        assert (
            operation.method,
            operation.path.split("{environment}")[1],
            operation.capability,
            operation.statuses,
        ) == (
            method,
            path,
            capability,
            statuses,
        ), identifier
    assert OPERATIONS["test_activations.run"].optional_idempotency is True
