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

"""Authenticated worker release/instance and task protocol endpoints."""

from typing import Any
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.workers import (
    ClaimRequest,
    CompleteRequest,
    CredentialGrantRequest,
    CredentialRequest,
    FailRequest,
    InstanceRequest,
    LeaseProof,
    ReleaseRequest,
)
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}"


@rest_controller
@request_mapping("")
class WorkerController:
    def __init__(self, service: WorkerService, tasks: TaskService) -> None:
        self.service, self.tasks = service, tasks

    def authority(self, request: Request) -> dict[str, Any]:
        return dict(
            actor=request.state.principal,
            scope=request_scope(request, environment=True),
            context=request.state.audit_context,
        )

    @operation("releases.create")
    async def release(self, request: Request) -> JSONResponse:
        result = await self.service.register_release(
            request.state.principal,
            request_scope(request, environment=True),
            ReleaseRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), status_code=201)

    @operation("workers.create")
    async def instance(self, request: Request) -> JSONResponse:
        result = await self.service.register_instance(
            request.state.principal,
            request_scope(request, environment=True),
            InstanceRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("workers.revoke")
    async def revoke(self, request: Request) -> JSONResponse:
        await self.service.revoke(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse({"revoked": True})

    @operation("workers.grant")
    async def grant(self, request: Request) -> JSONResponse:
        await self.service.grant_connection(
            request.state.principal,
            request_scope(request, environment=True),
            CredentialGrantRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse({"granted": True}, status_code=201)

    @operation("tasks.claim")
    async def claim(self, request: Request) -> JSONResponse:
        body = ClaimRequest.model_validate_json(await request.body())
        authority = self.authority(request)
        async with self.service.definitions.transaction(authority["scope"], None) as tx:
            result = await self.tasks.claim(tx, body.worker_id, body.limit, **authority)
        return JSONResponse([lease.model_dump(mode="json") for lease in result])

    @operation("tasks.heartbeat")
    async def heartbeat(self, request: Request) -> JSONResponse:
        body = LeaseProof.model_validate_json(await request.body())
        authority = self.authority(request)
        async with self.service.definitions.transaction(authority["scope"], None) as tx:
            result = await self.tasks.heartbeat(tx, body, **authority)
        return JSONResponse(result.model_dump(mode="json"))

    @operation("tasks.complete")
    async def complete(self, request: Request) -> JSONResponse:
        body = CompleteRequest.model_validate_json(await request.body())
        authority = self.authority(request)
        async with self.service.definitions.transaction(authority["scope"], None) as tx:
            result = await self.tasks.complete(tx, body.lease, body.completion_id, body.output, **authority)
        return JSONResponse(result.model_dump(mode="json"))

    @operation("tasks.fail")
    async def fail(self, request: Request) -> JSONResponse:
        body = FailRequest.model_validate_json(await request.body())
        authority = self.authority(request)
        async with self.service.definitions.transaction(authority["scope"], None) as tx:
            result = await self.tasks.fail(tx, body.lease, body.error, **authority)
        return JSONResponse(result.model_dump(mode="json"))

    @operation("tasks.credentials")
    async def credentials(self, request: Request) -> JSONResponse:
        result = await self.tasks.credentials(
            CredentialRequest.model_validate_json(await request.body()), **self.authority(request)
        )
        # Deliberate secret wire boundary; ordinary model serializers exclude value.
        return JSONResponse(
            {**result.model_dump(mode="json"), "value": result.value},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @operation("workers.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "workers")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "workers", request.url.path))

    @operation("releases.list")
    async def releases(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "worker-releases")
        result = await self.service.list(
            request.state.principal,
            scope,
            releases=True,
            limit=limit,
            cursor=cursor,
            context=request.state.audit_context,
        )
        return JSONResponse(page_response(result, scope, "worker-releases", request.url.path))

    @operation("workers.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("releases.read")
    async def read_release(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            releases=True,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
