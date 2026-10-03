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

"""Native PyFly controller composition with a loopback session and request boundary."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container.stereotypes import rest_controller
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.web import delete_mapping, get_mapping, post_mapping, request_mapping
from pyfly.web.adapters.starlette.app import create_app
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from firefly_weave.compiler.api import validate_authoring
from firefly_weave.sdk.auth import AuthError
from firefly_weave.sdk.sign_in import WorkspaceOption
from firefly_weave.studio.connection import (
    ConnectionConfigure,
    DiscoverRequest,
    LoginStart,
    PreferencesUpdate,
    ProfileCreate,
    ProfileRemoval,
    ProfileSelection,
    SignOut,
    StudioConnectionService,
    request_problem,
)
from firefly_weave.studio.service import BODY_LIMIT, COOKIE, StudioOptions, StudioService, problem, secret_matches


async def read_body[Body: BaseModel](request: Request, model: type[Body], *, optional: bool = False) -> Body | None:
    """A strict, bounded JSON body; an empty optional body takes the model defaults."""
    raw = await request.body()
    if len(raw) > 65536:
        return None
    if optional and not raw.strip():
        return model()
    try:
        return model.model_validate_json(raw)
    except (ValidationError, ValueError):
        return None


def _name(value: Any) -> str:
    return value if isinstance(value, str) else ""


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=256)


class ScopeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenantId: UUID
    projectId: UUID
    environmentId: UUID


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(max_length=1024 * 1024)
    format: Literal["yaml", "json"]
    filename: str | None = Field(default=None, max_length=256)
    strict: bool = False


class OpenAPIInventoryRequest(BaseModel):
    """Pasted or uploaded OpenAPI text; there is deliberately no URL field (no fetch from Studio's host)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(max_length=BODY_LIMIT)
    format: Literal["yaml", "json"]
    relaxations: dict[str, Any] | None = None


class OpenAPIImportRequest(OpenAPIInventoryRequest):
    """Either a reviewed policy, or a selection the host scaffolds a policy for; built-in Actions only."""

    policy: dict[str, Any] | None = None
    selection: list[str] | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=64)
    target: Literal["builtin"] = "builtin"

    def mixed(self) -> bool:
        """A reviewed policy carries its own operations, name and relaxations; never ignore fields silently."""
        return self.policy is not None and (
            self.selection is not None or self.relaxations is not None or self.name is not None
        )


class HttpActionBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request: dict[str, Any]


LOCAL_WORK_SECONDS = 30
LOCAL_WORKERS = 2


class _LocalWork:
    """Worker slots for local analysis. A thread cannot be cancelled, so the thread itself frees its
    slot when the work ends, not the request that gave up waiting: abandoned analyses stay bounded."""

    def __init__(self) -> None:
        import threading

        self.lock = threading.Lock()
        self.running = 0

    def claim(self) -> bool:
        with self.lock:
            if self.running >= LOCAL_WORKERS:
                return False
            self.running += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.running -= 1


_LOCAL_WORK = _LocalWork()


def _settled(future: asyncio.Future[Any]) -> None:
    # Retrieve the outcome so an abandoned analysis that failed is not reported as unhandled.
    if not future.cancelled():
        future.exception()


async def local_work(
    work: Any, *, slow: str = "The document took too long to analyze; select fewer operations"
) -> Response:
    """Run pure, CPU-bound authoring work off the event loop with a bounded wait; never any I/O."""
    from firefly_weave.compiler.canonical import canonical_bytes

    if not _LOCAL_WORK.claim():
        return problem(503, "WV-STUDIO-BUSY", "Studio is still analyzing an earlier document; try again shortly")

    def run() -> Any:
        try:
            return work()
        finally:
            _LOCAL_WORK.release()

    future = asyncio.get_running_loop().run_in_executor(None, run)
    future.add_done_callback(_settled)
    try:
        async with asyncio.timeout(LOCAL_WORK_SECONDS):
            value = await asyncio.shield(future)
    except TimeoutError:
        return problem(503, "WV-STUDIO-BUSY", slow)
    return Response(canonical_bytes(value), media_type="application/json")


@rest_controller
@request_mapping("")
class StudioController:
    def __init__(self, service: StudioService, connection: StudioConnectionService) -> None:
        self.service = service
        self.connection = connection

    @get_mapping("/studio/session")
    async def session(self, request: Request) -> JSONResponse:
        return JSONResponse(self.service.session_payload(self.service.authenticated(request)))

    @post_mapping("/studio/session")
    async def pair(self, request: Request) -> Response:
        try:
            body = PairRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Enter the pairing code shown in your terminal")
        return self.service.pair(body.code)

    @delete_mapping("/studio/session")
    async def logout(self, request: Request) -> Response:
        await self.connection.close()
        self.service.connection_generation += 1
        self.service.session = self.service.csrf = ""
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @get_mapping("/studio/connection")
    async def connection_status(self, request: Request) -> JSONResponse:
        return JSONResponse(await self.connection.status())

    @post_mapping("/studio/connection/discover")
    async def discover_platform(self, request: Request) -> Response:
        body = await read_body(request, DiscoverRequest)
        if body is None:
            return request_problem("Enter the server address of your platform.")
        return await self.connection.discover(body)

    @post_mapping("/studio/connection/profiles")
    async def save_platform(self, request: Request) -> Response:
        body = await read_body(request, ProfileCreate)
        if body is None:
            return request_problem("Review the platform details and confirm that you trust them before saving.")
        return await self.connection.create_profile(body)

    @post_mapping("/studio/connection/activate")
    async def activate_platform(self, request: Request) -> Response:
        body = await read_body(request, ProfileSelection)
        if body is None:
            return request_problem("Choose a saved platform.")
        return await self.connection.activate(body)

    @post_mapping("/studio/connection/disconnect")
    async def disconnect_platform(self, request: Request) -> Response:
        return await self.connection.disconnect()

    @post_mapping("/studio/connection/remove")
    async def remove_platform(self, request: Request) -> Response:
        body = await read_body(request, ProfileRemoval)
        if body is None:
            return request_problem("Choose a saved platform to remove.")
        return await self.connection.remove(body)

    @post_mapping("/studio/connection/logout")
    async def sign_out(self, request: Request) -> Response:
        body = await read_body(request, SignOut, optional=True)
        if body is None:
            return request_problem("Studio sent an incomplete request. Refresh Studio and try again.")
        return await self.connection.logout(body)

    @post_mapping("/studio/connection/configure")
    async def configure_connection(self, request: Request) -> Response:
        raw = await request.body()
        if len(raw) > 65536:
            return problem(413, "WV-STUDIO-CONNECTION", "Login configuration exceeds its bound")
        try:
            body = ConnectionConfigure.model_validate_json(raw)
        except (ValidationError, ValueError, AuthError):
            return problem(
                422, "WV-STUDIO-CONNECTION", "Review an explicit nonsecret login configuration and confirm trust"
            )
        return await self.connection.configure(body)

    @post_mapping("/studio/connection/test")
    async def test_connection(self, request: Request) -> Response:
        return await self.connection.test(request)

    @post_mapping("/studio/connection/login/start")
    async def start_login(self, request: Request) -> Response:
        body = await read_body(request, LoginStart, optional=True)
        if body is None:
            return request_problem("Choose how to sign in: in the browser or with a code.")
        return await self.connection.start(body.flow, body.switch_account)

    @get_mapping("/studio/connection/login/{identifier}")
    async def login_status(self, request: Request) -> Response:
        return self.connection.login_status(request.path_params["identifier"])

    @post_mapping("/studio/connection/login/{identifier}/cancel")
    async def cancel_login(self, request: Request) -> Response:
        return await self.connection.cancel(request.path_params["identifier"])

    @post_mapping("/studio/connection/login/{identifier}/open")
    async def open_login(self, request: Request) -> Response:
        return await self.connection.open_login(request.path_params["identifier"])

    @post_mapping("/studio/preferences")
    async def save_preferences(self, request: Request) -> Response:
        body = await read_body(request, PreferencesUpdate)
        if body is None:
            return request_problem("Choose whether Studio asks how to start or starts working locally.")
        return await self.connection.save_preferences(body)

    @post_mapping("/studio/scope")
    async def select_scope(self, request: Request) -> Response:
        try:
            selected = ScopeSelection.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-SCOPE", "Choose a tenant, project and environment from your platform")

        async def empty() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        discovery_request = Request({**request.scope, "method": "GET", "query_string": b""}, empty)
        generation = self.service.connection_generation
        response = await self.service.bridge(discovery_request, "/api/v1/identity")
        if response.status_code != 200:
            return response
        try:
            identity = json.loads(bytes(response.body))
            found = next(
                (
                    (w, p, e)
                    for w in identity["workspaces"]
                    for p in w["projects"]
                    for e in p["environments"]
                    if w["id"] == str(selected.tenantId)
                    and p["id"] == str(selected.projectId)
                    and e["id"] == str(selected.environmentId)
                ),
                None,
            )
        except (ValueError, KeyError, TypeError):
            return problem(502, "WV-STUDIO-IDENTITY", "Platform discovery returned an incompatible response")
        if found is None:
            return problem(403, "WV-STUDIO-SCOPE", "This environment is not in your authorized workspace list")
        if generation != self.service.connection_generation:
            return problem(
                409, "WV-STUDIO-CONNECTION-CHANGED", "Connection changed; choose a scope from the new profile"
            )
        tenant, project, environment = (_name(level.get("name")) for level in found)
        # Names are for display only; ones a profile cannot store safely are left out.
        selection = WorkspaceOption(
            tenant_id=selected.tenantId,
            tenant_name=tenant,
            project_id=selected.projectId,
            project_name=project,
            environment_id=selected.environmentId,
            environment_name=environment,
            label=f"{tenant} / {project} / {environment}",
        ).selection()
        return await self.connection.apply_scope(generation, selection)

    @get_mapping("/studio/contracts/deployment-target")
    async def deployment_target_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.deployments import TargetRequest

        return JSONResponse(TargetRequest.model_json_schema(), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/deployment-target-update")
    async def deployment_target_update_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.deployments import TargetUpdate

        return JSONResponse(TargetUpdate.model_json_schema(), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/deployment")
    async def deployment_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.deployments import DeploymentRequest

        return JSONResponse(DeploymentRequest.model_json_schema(), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/llm-profile")
    async def llm_profile_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.llm import LLMProfile

        return JSONResponse(LLMProfile.model_json_schema(by_alias=True), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/file-reference")
    async def file_reference_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.files import file_reference_schema

        return JSONResponse(file_reference_schema(), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/lumi-configuration")
    async def lumi_configuration_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.lumi import LumiConfigurationRequest

        return JSONResponse(
            LumiConfigurationRequest.model_json_schema(by_alias=True), headers={"Cache-Control": "no-store"}
        )

    @post_mapping("/studio/local/validate")
    async def validate(self, request: Request) -> Response:
        try:
            body = ValidationRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Provide a bounded source string and YAML or JSON format")
        # Flow and type analysis of a bounded document can take seconds: never on the event loop.
        # The result is already canonical JSON, so re-encoding it returns the same bytes.
        return await local_work(
            lambda: json.loads(validate_authoring(body.source, format=body.format, filename=body.filename).to_bytes()),
            slow="Checking this definition took too long; simplify it and try again",
        )

    @post_mapping("/studio/local/openapi/inventory")
    async def openapi_inventory(self, request: Request) -> Response:
        from firefly_weave.sdk.openapi_import import inventory

        try:
            body = OpenAPIInventoryRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Paste or upload an OpenAPI document as JSON or YAML text")

        def work() -> Any:
            listing = inventory(body.source, source_format=body.format, relaxations=body.relaxations)
            return listing.model_dump(by_alias=True, mode="json")

        return await local_work(work)

    @post_mapping("/studio/local/openapi/import")
    async def openapi_import(self, request: Request) -> Response:
        from firefly_weave.sdk.openapi_import import import_openapi, init_policy

        try:
            body = OpenAPIImportRequest.model_validate_json(await request.body())
        except ValidationError:
            body = None
        if body is None or body.mixed():
            return problem(
                422, "WV-STUDIO-REQUEST", "Provide the OpenAPI text and a reviewed policy or a selection of operations"
            )

        def work() -> Any:
            policy, notes = body.policy, []
            if policy is None:
                scaffold = init_policy(
                    body.source,
                    body.selection,
                    source_format=body.format,
                    relaxations=body.relaxations,
                    name=body.name,
                )
                if not scaffold.ok or scaffold.policy is None:
                    return {
                        "ok": False,
                        "actions": [],
                        "policy": None,
                        "diagnostics": [d.model_dump(by_alias=True, mode="json") for d in scaffold.diagnostics],
                    }
                policy, notes = scaffold.policy, scaffold.diagnostics
            result = import_openapi(
                body.source, None, policy, source_format=body.format, target="builtin", all_diagnostics=True
            )
            seen = {(d.code, d.path) for d in result.diagnostics}
            diagnostics = list(result.diagnostics) + [d for d in notes if (d.code, d.path) not in seen]
            return {
                **result.model_dump(by_alias=True, mode="json", exclude={"diagnostics", "connector", "package"}),
                "policy": policy,
                "diagnostics": [d.model_dump(by_alias=True, mode="json") for d in diagnostics],
            }

        return await local_work(work)

    @post_mapping("/studio/local/http-action")
    async def http_action(self, request: Request) -> Response:
        from firefly_weave.sdk.http_actions import author_http_action

        try:
            body = HttpActionBuild.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Describe the HTTP request to turn into an action")
        return await local_work(lambda: author_http_action(body.request).model_dump(by_alias=True, mode="json"))


class StudioBoundary:
    """Reject rebinding/cross-site access before credentials or compiler work is reached."""

    def __init__(self, app: ASGIApp, service: StudioService) -> None:
        self.app, self.service = app, service
        self.slots = asyncio.Semaphore(8)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        headers = request.headers

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                security = {
                    b"cache-control": b"no-store",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"cross-origin-resource-policy": b"same-origin",
                    b"content-security-policy": (
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        b"img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                        b"base-uri 'self'; form-action 'self'"
                    ),
                }
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() not in security
                ] + list(security.items())
            await send(message)

        async def reject(status: int, code: str, message: str) -> None:
            await problem(status, code, message)(scope, receive, secure_send)

        if (
            headers.get("host") != urlsplit(self.service.options.origin).netloc
            or headers.get("sec-fetch-site") == "cross-site"
            or headers.get("origin") not in {None, self.service.options.origin}
        ):
            await reject(403, "WV-STUDIO-ORIGIN", "Open Studio at its exact local address")
            return
        mutation = request.method not in {"GET", "HEAD"}
        if mutation and headers.get("origin") != self.service.options.origin:
            await reject(403, "WV-STUDIO-ORIGIN", "A same-origin Studio request is required")
            return
        public_session = request.url.path == "/studio/session" and request.method in {"GET", "POST"}
        protected = request.url.path.startswith("/studio/") and not public_session
        if protected and not self.service.authenticated(request):
            await reject(401, "WV-STUDIO-SESSION", "Pair this browser using the code in your terminal")
            return
        if protected and mutation and not secret_matches(headers.get("x-weave-csrf", ""), self.service.csrf):
            await reject(403, "WV-STUDIO-CSRF", "Refresh Studio before submitting this change")
            return
        if self.slots.locked():
            await reject(429, "WV-STUDIO-BUSY", "Studio is busy; wait for the current requests")
            return
        async with self.slots:
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > BODY_LIMIT:
                            await reject(413, "WV-STUDIO-BODY", "Request exceeds Studio's size limit")
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408, "WV-STUDIO-TIMEOUT", "Request body timed out")
                return
            consumed = False

            async def bounded_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, secure_send)


@pyfly_application(name="firefly-weave-studio", scan_packages=["firefly_weave.studio.host"])
class StudioApplication:
    pass


def make_studio_app(options: StudioOptions) -> Starlette:
    if any(name.startswith("PYFLY_") for name in os.environ):
        raise ValueError("Use Studio options; PYFLY_* overrides are not supported")
    pyfly = PyFlyApplication(StudioApplication, config_path=Path(__file__).with_name("pyfly.yaml"))
    service = StudioService(options)
    connection = StudioConnectionService(service)
    service.connection = connection
    pyfly.context.container.register_instance(StudioOptions, options)
    pyfly.context.container.register_instance(StudioService, service)
    pyfly.context.container.register_instance(StudioConnectionService, connection)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await pyfly.startup()
            pyfly.context.get_bean(StudioController)
            yield
        finally:
            await connection.close()
            await service.close()
            await pyfly.shutdown()

    app = create_app(
        title="Firefly Weave Studio",
        context=pyfly.context,
        lifespan=lifespan,
        docs_enabled=False,
        actuator_enabled=False,
    )

    async def bridge(request: Request) -> Response:
        return await service.bridge(request, "/" + request.path_params["path"])

    async def asset(request: Request) -> Response:
        path = request.path_params.get("path", "")
        candidate = (options.assets / path).resolve()
        if not candidate.is_relative_to(options.assets.resolve()) or candidate.is_symlink():
            return problem(404, "WV-STUDIO-ASSET", "Asset not found")
        if not candidate.is_file():
            if Path(path).suffix or path.startswith("studio/"):
                return problem(404, "WV-STUDIO-ASSET", "Asset not found")
            candidate = options.assets / "index.html"
        if candidate.suffix not in {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff2"}:
            return problem(404, "WV-STUDIO-ASSET", "Asset not found")
        return FileResponse(candidate)

    app.router.routes.extend(
        [
            Route("/studio/api/{path:path}", bridge, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
            Route("/{path:path}", asset, methods=["GET"]),
        ]
    )
    app.add_middleware(StudioBoundary, service=service)
    app.state.pyfly = pyfly
    app.state.studio = service
    app.state.studio_connection = connection
    return app
