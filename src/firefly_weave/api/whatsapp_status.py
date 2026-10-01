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
"""Scoped WhatsApp reads through the exact operator-selected native declaration."""

from typing import TYPE_CHECKING, cast
from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.connectors.builtin_catalog import BuiltinPackageMetadata
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.definitions.models import CatalogError
from firefly_weave.providers.service import ProviderIngressService

if TYPE_CHECKING:
    from firefly_weave.providers.whatsapp import WhatsAppVerifier


@rest_controller
@request_mapping("")
class WhatsAppStatusController:
    def __init__(self, ingress: ProviderIngressService) -> None:
        self.ingress = ingress

    async def verifier(self, request: Request) -> "WhatsAppVerifier":
        source = cast(
            ProviderSource,
            await self.ingress.read(
                request.state.principal,
                request_scope(request, environment=True),
                UUID(request.path_params["identifier"]),
                context=request.state.audit_context,
            ),
        )
        package = self.ingress.registry.provider_package(
            source.package, source.package_version, source.provider, source.schema_digest, source.adapter_version
        )
        if (
            source.provider != "whatsapp"
            or source.package != "firefly-weave"
            or type(package.metadata) is not BuiltinPackageMetadata
            or package.metadata.model.manifest.spec.adapter != "weave-whatsapp"
        ):
            raise CatalogError(404, "WV-NOT-FOUND", "WhatsApp status not found")
        return cast("WhatsAppVerifier", self.ingress.registry.builtin_verifier("weave-whatsapp"))

    @operation("whatsapp_statuses.read")
    async def read(self, request: Request) -> JSONResponse:
        if len(request.query_params.getlist("message_id")) != 1 or set(request.query_params) != {"message_id"}:
            raise ValueError("One message identifier is required")
        verifier = await self.verifier(request)
        result = await verifier.statuses.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            request.query_params["message_id"],
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("whatsapp_statuses.facts")
    async def facts(self, request: Request) -> JSONResponse:
        verifier = await self.verifier(request)
        scope = request_scope(request, environment=True)
        collection = (
            "whatsapp_status_facts:" + request.path_params["identifier"] + ":" + request.path_params["state_id"]
        )
        limit, cursor = page_request(request, scope, collection)
        result = await verifier.statuses.facts(
            request.state.principal,
            scope,
            UUID(request.path_params["identifier"]),
            UUID(request.path_params["state_id"]),
            context=request.state.audit_context,
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(page_response(result, scope, collection, request.url.path))
