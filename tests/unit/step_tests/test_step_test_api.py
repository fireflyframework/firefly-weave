# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""The step test and test policy routes: what each one sends to its service and answers."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.api.step_tests import StepTestController
from firefly_weave.api.test_policy import TestPolicyController
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.step_tests import EnvironmentTestPolicy, StepTestAccepted
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.definitions.models import CatalogError

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
TESTER = Principal(id=uuid4(), kind="human", grants=(Grant(role="step_tester", scope=SCOPE),))
VIEWER = Principal(id=uuid4(), kind="human", grants=(Grant(role="viewer", scope=SCOPE),))


def request(body=None, headers=None, actor=TESTER, identifier=None):
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
        "method": "POST",
        "path": f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}/x",
        "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
        "query_string": b"",
        "path_params": params,
        "state": {"principal": actor, "audit_context": AuditContext()},
    }
    return Request(scope, receive)


BODY = {
    "kind": "action",
    "step_id": "fetch-customer",
    "input": {"customerId": "C-1"},
    "draft_id": str(uuid4()),
    "draft_revision": 1,
    "timeout_seconds": 60,
}


async def test_creating_a_step_test_answers_202_with_its_run_and_sends_the_key():
    accepted = StepTestAccepted(
        id=uuid4(), run_id=uuid4(), status="running", side_effect="read_only", expires_at=datetime.now(UTC)
    )
    seen = []

    async def start(actor, scope, body, key, *, context):
        seen.append((actor, scope, body.step_id, key))
        return accepted

    controller = StepTestController(SimpleNamespace(start=start), AuthorizationService())
    answer = await controller.create(request(BODY, {"Idempotency-Key": "click-1"}))
    assert answer.status_code == 202
    assert json.loads(answer.body) == accepted.model_dump(mode="json")
    assert seen == [(TESTER, SCOPE, "fetch-customer", "click-1")]


def test_creating_a_step_test_needs_an_idempotency_key_and_answers_202():
    operation = OPERATIONS["step_tests.create"]
    assert (operation.idempotency, operation.statuses, operation.capability) == (True, (202,), "step.test")


async def test_the_answers_endpoint_is_published_but_not_served():
    controller = StepTestController(SimpleNamespace(), AuthorizationService())
    with pytest.raises(CatalogError) as unserved:
        await controller.answer(request({"instance_key": "ask", "confirm": False}, identifier=uuid4()))
    assert (unserved.value.status, unserved.value.code) == (501, "WV-UNAVAILABLE")
    with pytest.raises(AccessDenied):
        await controller.answer(request({"instance_key": "ask", "confirm": False}, actor=VIEWER, identifier=uuid4()))
    assert OPERATIONS["step_tests.answer"].served is False


def test_an_omitted_output_is_published_as_omitted():
    schema = OPERATIONS["step_tests.read"].response.model_json_schema()
    assert "omitted" in schema["properties"]
    assert "omitted" not in schema.get("required", [])


@pytest.mark.parametrize("header, expected", [(None, 0), ('"3"', 3), ("4", 4)])
async def test_a_policy_save_sends_the_revision_it_read_or_none_before_the_first(header, expected):
    seen = []

    async def save(actor, scope, update, revision, *, context):
        seen.append((update.test_calls, revision))
        return EnvironmentTestPolicy(test_calls=update.test_calls, revision=revision + 1)

    controller = TestPolicyController(SimpleNamespace(save=save))
    answer = await controller.save(request({"test_calls": "enabled"}, {"If-Match": header} if header else {}))
    assert seen == [("enabled", expected)]
    assert answer.headers["etag"] == f'"{expected + 1}"'


@pytest.mark.parametrize("header", ['"0"', "0", 'W/"2"', "-1"])
async def test_a_policy_save_with_an_invalid_revision_is_refused(header):
    async def save(*args, **kwargs):
        raise AssertionError("never saved")

    controller = TestPolicyController(SimpleNamespace(save=save))
    with pytest.raises(CatalogError) as refused:
        await controller.save(request({"test_calls": "enabled"}, {"If-Match": header}))
    assert (refused.value.status, refused.value.code) == (422, "WV-ETAG")
