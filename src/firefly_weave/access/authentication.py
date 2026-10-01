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

"""Bearer authentication; no static service-key or provider-administration path."""

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pyfly.container import service
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.providers.base import AccessTokenVerifier, PrincipalResolver


@dataclass(frozen=True)
class VerifierSet:
    """Trusted operator-provided verifiers, configured before application startup."""

    items: tuple[AccessTokenVerifier, ...]


@service
class AuthenticationService:
    def __init__(self, verifiers: VerifierSet, resolver: PrincipalResolver) -> None:
        self.verifiers = verifiers
        self.resolver = resolver

    async def authenticate(self, credential: str) -> Principal:
        # Only configured providers are tried; unverified token claims select no endpoint.
        for verifier in self.verifiers.items:
            try:
                identity = await verifier.verify(credential)
            except AuthenticationFailed:
                continue
            return await self.resolver.resolve(identity)
        raise AuthenticationFailed()


@order(HIGHEST_PRECEDENCE + 250)
class AuthenticationFilter(OncePerRequestFilter):
    def __init__(self, authentication: AuthenticationService) -> None:
        self.authentication = authentication

    @staticmethod
    def finish(request: Request, response: Any) -> Any:
        request_id = str(request.state.audit_context.request_id)
        if response.status_code >= 400:
            try:
                body = json.loads(response.body)
            except (ValueError, TypeError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            result = body.get("result")
            body = {
                "status": response.status_code,
                "code": body.get("code", "WV-NOT-FOUND" if response.status_code == 404 else "WV-REQUEST"),
                "message": body.get("message", "Request failed"),
                "request_id": request_id,
                "diagnostics": result.get("diagnostics", []) if isinstance(result, dict) else [],
                **({"result": result} if result is not None else {}),
            }
            response = JSONResponse(
                body,
                status_code=response.status_code,
                headers={
                    k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-type"}
                },
                media_type="application/problem+json",
            )
        response.headers["X-Weave-Request-ID"] = request_id
        response.headers["X-Weave-Wire-Version"] = "weave/api-v1"
        return response

    async def do_filter(self, request: Request, call_next: CallNext) -> Any:
        request.state.audit_context = AuditContext(request_id=uuid4())
        if (
            (
                getattr(request.app.state, "weave_docs_enabled", False)
                and request.method in {"GET", "HEAD"}
                and request.url.path.removeprefix(request.scope.get("root_path", "").rstrip("/"))
                in {"/docs", "/openapi.json"}
            )
            or (
                request.method in {"GET", "POST"}
                and re.fullmatch(
                    r"/provider-ingress/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request.url.path
                )
            )
            or request.url.path in {"/health/live", "/health/ready"}
            or (
                request.method == "POST"
                and re.fullmatch(r"/webhooks/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request.url.path)
            )
        ):
            return self.finish(request, await call_next(request))
        try:
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not value or " " in value:
                raise AuthenticationFailed()
            principal = await self.authentication.authenticate(value)
        except AuthenticationFailed:
            return self.finish(
                request,
                JSONResponse(
                    {"code": "WV-UNAUTHENTICATED", "message": "Authentication failed"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                ),
            )
        except Exception:
            return self.finish(
                request, JSONResponse({"code": "WV-INTERNAL", "message": "Request failed"}, status_code=500)
            )
        request.state.principal = principal
        context = SecurityContext(user_id=str(principal.id))
        request.state.security_context = context
        current = RequestContext.current()
        if current is not None:
            current.security_context = context
            current.set("weave.principal", principal)
        response = await call_next(request)
        return self.finish(request, response)
