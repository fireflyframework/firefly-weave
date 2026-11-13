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
"""Whether an environment allows test calls: read it, and change it with the revision you read."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, parse_revision
from firefly_weave.contracts.step_tests import EnvironmentTestPolicy, EnvironmentTestPolicyUpdate
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests.policy import TestPolicyService


def _answer(policy: EnvironmentTestPolicy, request: Request) -> JSONResponse:
    headers = {"ETag": etag(policy.revision, request.url.path)} if policy.revision else {}
    return JSONResponse(policy.model_dump(mode="json"), headers=headers)


@rest_controller
@request_mapping("")
class TestPolicyController:
    __test__ = False

    def __init__(self, service: TestPolicyService) -> None:
        self.service = service

    @operation("test_policy.read")
    async def read(self, request: Request) -> JSONResponse:
        policy = await self.service.read(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return _answer(policy, request)

    @operation("test_policy.save")
    async def save(self, request: Request) -> JSONResponse:
        try:
            revision = parse_revision(request.headers.get("If-Match"))
        except ValueError:
            raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision") from None
        update = EnvironmentTestPolicyUpdate.model_validate_json(await request.body())
        policy = await self.service.save(
            request.state.principal,
            request_scope(request, environment=True),
            update,
            # No If-Match: the environment has no stored policy yet, which reads as revision 0.
            revision or 0,
            context=request.state.audit_context,
        )
        return _answer(policy, request)
