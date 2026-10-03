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

"""Native typed Operations endpoints; the browser never supplies executable code."""

from typing import Any
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, page_request, page_response, parse_revision
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    DeploymentLeaseProof,
    DeploymentRequest,
    JobCancelRequest,
    ObserveRequest,
    PlanApprovalRequest,
    PlanRequest,
    ReconcileJobRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.deployments.service import DeploymentService


@rest_controller
@request_mapping("")
class DeploymentController:
    def __init__(self, service: DeploymentService):
        self.service = service

    def authority(self, request: Request) -> dict[str, Any]:
        return dict(
            actor=request.state.principal,
            scope=request_scope(request, environment=True),
            context=request.state.audit_context,
        )

    async def listing(self, request: Request, collection: str) -> JSONResponse:
        authority = self.authority(request)
        limit, after = page_request(request, authority["scope"], collection)
        target = request.query_params.get("target_id")
        deployment = request.query_params.get("deployment_id") if collection == "plans" else None
        result = await self.service.list(
            collection,
            limit=limit,
            after=after,
            target_id=UUID(target) if target else None,
            deployment_id=UUID(deployment) if deployment else None,
            **authority,
        )
        return JSONResponse(page_response(result, authority["scope"], collection, request.url.path))

    async def reading(self, request: Request, collection: str) -> JSONResponse:
        result = await self.service.read(collection, UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deployment_targets.list")
    async def deployment_targets_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "targets")

    @operation("deployment_targets.read")
    async def deployment_targets_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "targets")

    @operation("deployments.list")
    async def deployments_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "deployments")

    @operation("deployments.read")
    async def deployments_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "deployments")

    @operation("deployment_observations.list")
    async def deployment_observations_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "observations")

    @operation("deployment_observations.read")
    async def deployment_observations_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "observations")

    @operation("deployment_plans.list")
    async def deployment_plans_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "plans")

    @operation("deployment_plans.read")
    async def deployment_plans_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "plans")

    @operation("deployment_jobs.list")
    async def deployment_jobs_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "jobs")

    @operation("deployment_jobs.read")
    async def deployment_jobs_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "jobs")

    @operation("deployment_runners.list")
    async def deployment_runners_list(self, request: Request) -> JSONResponse:
        return await self.listing(request, "runners")

    @operation("deployment_runners.read")
    async def deployment_runners_read(self, request: Request) -> JSONResponse:
        return await self.reading(request, "runners")

    @operation("deployment_targets.create")
    async def deployment_targets_create(self, request: Request) -> JSONResponse:
        result = await self.service.create_target(
            TargetRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployment_targets.update")
    async def deployment_targets_update(self, request: Request) -> JSONResponse:
        result = await self.service.update_target(
            UUID(request.path_params["identifier"]),
            TargetUpdate.model_validate_json(await request.body()),
            int(parse_revision(request.headers.get("If-Match"), required=True) or 0),
            **self.authority(request),
        )
        return JSONResponse(
            result.model_dump(mode="json") if result is not None else None,
            status_code=200,
            headers={"ETag": etag(result.revision, request.url.path)},
        )

    @operation("deployments.create")
    async def deployments_create(self, request: Request) -> JSONResponse:
        result = await self.service.create_deployment(
            DeploymentRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployments.update")
    async def deployments_update(self, request: Request) -> JSONResponse:
        result = await self.service.update_deployment(
            UUID(request.path_params["identifier"]),
            DeploymentRequest.model_validate_json(await request.body()),
            int(parse_revision(request.headers.get("If-Match"), required=True) or 0),
            **self.authority(request),
        )
        return JSONResponse(
            result.model_dump(mode="json") if result is not None else None,
            status_code=200,
            headers={"ETag": etag(result.revision, request.url.path)},
        )

    @operation("deployment_observations.create")
    async def deployment_observations_create(self, request: Request) -> JSONResponse:
        result = await self.service.observe(
            ObserveRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployment_plans.create")
    async def deployment_plans_create(self, request: Request) -> JSONResponse:
        result = await self.service.plan(
            PlanRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployment_plans.approve")
    async def deployment_plans_approve(self, request: Request) -> JSONResponse:
        result = await self.service.approve(
            UUID(request.path_params["identifier"]),
            PlanApprovalRequest.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_plans.apply")
    async def deployment_plans_apply(self, request: Request) -> JSONResponse:
        result = await self.service.apply(
            UUID(request.path_params["identifier"]),
            ApplyPlanRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployment_jobs.cancel")
    async def deployment_jobs_cancel(self, request: Request) -> JSONResponse:
        result = await self.service.cancel(
            UUID(request.path_params["identifier"]),
            JobCancelRequest.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_runners.create")
    async def deployment_runners_create(self, request: Request) -> JSONResponse:
        result = await self.service.register_runner(
            RunnerRegistration.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=201)

    @operation("deployment_runners.claim")
    async def deployment_runners_claim(self, request: Request) -> JSONResponse:
        result = await self.service.claim(
            RunnerClaimRequest.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_runners.renew")
    async def deployment_runners_renew(self, request: Request) -> JSONResponse:
        result = await self.service.renew(
            DeploymentLeaseProof.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_runners.report")
    async def deployment_runners_report(self, request: Request) -> JSONResponse:
        result = await self.service.report(
            RunnerReport.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_runners.revoke")
    async def deployment_runners_revoke(self, request: Request) -> JSONResponse:
        result = await self.service.revoke_runner(UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse(result.model_dump(mode="json") if result is not None else None, status_code=200)

    @operation("deployment_jobs.reconcile")
    async def reconcile(self, request: Request) -> JSONResponse:
        result = await self.service.reconcile(
            UUID(request.path_params["identifier"]),
            ReconcileJobRequest.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deployment_plans.approval")
    async def approval(self, request: Request) -> JSONResponse:
        result = await self.service.approval(UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse(result.model_dump(mode="json") if result is not None else None)
