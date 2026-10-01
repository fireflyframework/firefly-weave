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

"""Native controllers delegate all access decisions to application services."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request

from firefly_weave.access.service import AccessService
from firefly_weave.api.surface import operation
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.public import GrantRequest, NameRequest


def request_scope(request: Request, *, environment: bool = False) -> Scope:
    values = request.path_params
    return Scope(
        tenant_id=UUID(values["tenant"]),
        project_id=UUID(values["project"]) if "project" in values else None,
        environment_id=UUID(values["environment"]) if environment else None,
    )


@rest_controller
@request_mapping("")
class AccessController:
    def __init__(self, service: AccessService) -> None:
        self.service = service

    @operation("admin.tenant")
    async def tenant(self, request: Request) -> dict[str, str]:
        body = NameRequest.model_validate_json(await request.body())
        return {
            "id": str(
                await self.service.create_tenant(
                    request.state.principal, body.name, context=request.state.audit_context
                )
            )
        }

    @operation("projects.create")
    async def project(self, request: Request) -> dict[str, str]:
        body = NameRequest.model_validate_json(await request.body())
        return {
            "id": str(
                await self.service.create_project(
                    request.state.principal, request_scope(request), body.name, context=request.state.audit_context
                )
            )
        }

    @operation("environments.create")
    async def environment(self, request: Request) -> dict[str, str]:
        body = NameRequest.model_validate_json(await request.body())
        return {
            "id": str(
                await self.service.create_environment(
                    request.state.principal, request_scope(request), body.name, context=request.state.audit_context
                )
            )
        }

    @operation("environments.read")
    async def get_environment(self, request: Request) -> dict[str, str]:
        result = await self.service.get_environment(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return {"id": str(result["id"]), "name": result["name"]}

    @operation("admin.grant")
    async def grant(self, request: Request) -> dict[str, str]:
        body = GrantRequest.model_validate_json(await request.body())
        return {
            "id": str(
                await self.service.grant(
                    request.state.principal,
                    body.principal_id,
                    body.grant,
                    context=request.state.audit_context,
                )
            )
        }
