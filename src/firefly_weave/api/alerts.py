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

"""Alert operations: rules, previews and presets. Every operation is authorized by its service."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, page_request, page_response, parse_revision
from firefly_weave.contracts.alerts import AlertRuleDraft, AlertRuleView
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_rules import AlertRuleService
from firefly_weave.operations.loop_health import LoopHealth


def _revision(request: Request) -> int | None:
    """The If-Match revision, if sent; a missing one is refused by the service after authorization."""
    try:
        return parse_revision(request.headers.get("If-Match"))
    except ValueError:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision") from None


def _tagged(rule: AlertRuleView, request: Request, status: int = 200) -> JSONResponse:
    return JSONResponse(
        rule.model_dump(mode="json"), status_code=status, headers={"ETag": etag(rule.revision, request.url.path)}
    )


@rest_controller
@request_mapping("")
class AlertController:
    def __init__(self, rules: AlertRuleService) -> None:
        self.rules = rules

    @operation("alert_rules.list")
    async def rules_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "alert-rules")
        result = await self.rules.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "alert-rules", request.url.path))

    @operation("alert_rules.read")
    async def rules_read(self, request: Request) -> JSONResponse:
        rule = await self.rules.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return _tagged(rule, request)

    @operation("alert_rules.create")
    async def rules_create(self, request: Request) -> JSONResponse:
        rule = await self.rules.create(
            request.state.principal,
            request_scope(request, environment=True),
            AlertRuleDraft.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return _tagged(rule, request, 201)

    @operation("alert_rules.update")
    async def rules_update(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        rule = await self.rules.update(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            AlertRuleDraft.model_validate_json(await request.body()),
            revision,
            context=request.state.audit_context,
        )
        return _tagged(rule, request)

    @operation("alert_rules.delete")
    async def rules_delete(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        revoked = await self.rules.delete(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            revision,
            context=request.state.audit_context,
        )
        return JSONResponse(revoked.model_dump(mode="json"))

    @operation("alert_rules.preview")
    async def rules_preview(self, request: Request) -> JSONResponse:
        # Health readings are this process's own; a process without background loops reports none degraded.
        health = getattr(request.app.state, "loop_health", None) or LoopHealth()
        preview = await self.rules.preview(
            request.state.principal,
            request_scope(request, environment=True),
            AlertRuleDraft.model_validate_json(await request.body()),
            health=health,
            context=request.state.audit_context,
        )
        return JSONResponse(preview.model_dump(mode="json"))

    @operation("alert_presets.list")
    async def presets_list(self, request: Request) -> JSONResponse:
        presets = await self.rules.presets(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse([preset.model_dump(mode="json") for preset in presets])
