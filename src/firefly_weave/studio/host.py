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
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container.stereotypes import rest_controller
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.web import delete_mapping, get_mapping, post_mapping, put_mapping, request_mapping
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

if TYPE_CHECKING:
    from firefly_weave.studio.debug_sessions import HostSessions


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


class StoreValue(BaseModel):
    """One value Studio keeps on this computer: text it already serialized."""

    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


class ScopeRequest(BaseModel):
    """A local definition and the RFC 6901 path of one expression in it."""

    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(max_length=1024 * 1024)
    format: Literal["yaml", "json"]
    path: str = Field(max_length=4096, pattern=r"^(?:/(?:[^~/]|~[01])*)*$")


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


# Room for a body sample and a response sample at the 256 KB Studio accepts for each, with their schemas.
HTTP_ACTION_LIMIT = 1024 * 1024
LOCAL_WORK_SECONDS = 30
LOCAL_WORKERS = 2


class _LocalWork:
    """Worker slots for local analysis. A thread cannot be cancelled, so the thread itself frees its
    slot when the work ends, not the request that gave up waiting: abandoned analyses stay bounded."""

    def __init__(self, slots: int = LOCAL_WORKERS) -> None:
        import threading

        self.lock = threading.Lock()
        self.running = 0
        self.slots = slots

    def claim(self) -> bool:
        with self.lock:
            if self.running >= self.slots:
                return False
            self.running += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.running -= 1


_LOCAL_WORK = _LocalWork()
# Simulated runs get their own slot, so a long one never starves validation's two.
_DEBUG_WORK = _LocalWork(1)


def _settled(future: asyncio.Future[Any]) -> None:
    # Retrieve the outcome so an abandoned analysis that failed is not reported as unhandled.
    if not future.cancelled():
        future.exception()


async def local_work(
    work: Any,
    *,
    slow: str = "The document took too long to analyze; select fewer operations",
    pool: _LocalWork = _LOCAL_WORK,
    busy: str = "Studio is still analyzing an earlier document; try again shortly",
    respond: Callable[[Any], Response] | None = None,
) -> Response:
    """Run pure, CPU-bound authoring work off the event loop with a bounded wait; never any I/O."""
    from firefly_weave.compiler.canonical import canonical_bytes

    if not pool.claim():
        return problem(503, "WV-STUDIO-BUSY", busy)

    def run() -> Any:
        try:
            return work()
        finally:
            pool.release()

    future = asyncio.get_running_loop().run_in_executor(None, run)
    future.add_done_callback(_settled)
    try:
        async with asyncio.timeout(LOCAL_WORK_SECONDS):
            value = await asyncio.shield(future)
    except TimeoutError:
        return problem(503, "WV-STUDIO-BUSY", slow)
    if respond is not None:
        return respond(value)
    return Response(canonical_bytes(value), media_type="application/json")


def _session_id(request: Request) -> UUID | None:
    try:
        return UUID(request.path_params["identifier"])
    except ValueError:
        return None


def _session_gone() -> Response:
    return problem(404, "WV-STUDIO-DEBUG-NOT-FOUND", "This run log isn't open anymore")


@rest_controller
@request_mapping("")
class StudioController:
    def __init__(self, service: StudioService, connection: StudioConnectionService) -> None:
        self.service = service
        self.connection = connection
        self._sessions: tuple[int, HostSessions] | None = None

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

    @post_mapping("/studio/contracts/llm-profile/validate")
    async def validate_llm_profile(self, request: Request) -> Response:
        from firefly_weave.contracts.llm import LLMProfile

        raw = await request.body()
        if len(raw) > 65536:
            return problem(413, "WV-STUDIO-REQUEST", "AI profile exceeds the 64 KiB limit")

        def work() -> Any:
            try:
                LLMProfile.model_validate_json(raw)
            except ValidationError as exc:
                # Return field locations and guidance without reflecting profile values.
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                return {
                    "valid": False,
                    "issues": [{"path": list(error["loc"]), "message": error["msg"]} for error in errors[:50]],
                }
            return {"valid": True, "issues": []}

        return await local_work(work)

    @get_mapping("/studio/contracts/file-reference")
    async def file_reference_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.files import file_reference_schema

        return JSONResponse(file_reference_schema(), headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/connector-actions")
    async def connector_actions_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.connector_actions import builtin_connector_actions

        return JSONResponse({"actions": builtin_connector_actions()}, headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/contracts/lumi-configuration")
    async def lumi_configuration_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.lumi import LumiConfigurationRequest

        return JSONResponse(
            LumiConfigurationRequest.model_json_schema(by_alias=True), headers={"Cache-Control": "no-store"}
        )

    @get_mapping("/studio/contracts/language")
    async def language_contract(self, request: Request) -> Response:
        from firefly_weave.contracts.language import language_manifest

        return JSONResponse(language_manifest().model_dump(mode="json"), headers={"Cache-Control": "no-store"})

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

    @post_mapping("/studio/local/scope")
    async def scope(self, request: Request) -> Response:
        try:
            body = ScopeRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(
                422, "WV-STUDIO-REQUEST", "Provide a bounded source string, YAML or JSON, and a JSON Pointer"
            )

        def work() -> Any:
            from firefly_weave.compiler.parser import ParseFailure, parse_source
            from firefly_weave.compiler.scope import ScopeResult, scope_at

            try:
                document = parse_source(body.source, format=body.format).value
            except ParseFailure:
                document = None
            if not isinstance(document, dict):
                return ScopeResult(found=False, evaluated=False, truncated=False, entries=[]).model_dump(
                    mode="json", by_alias=True
                )
            return scope_at(document, body.path).model_dump(mode="json", by_alias=True)

        return await local_work(work, slow="Finding the data for this field took too long; simplify the workflow")

    # --- the desktop store ----------------------------------------------------------------------------------------

    def _store(self) -> Any:
        return self.service.local_store

    @get_mapping("/studio/local/store")
    async def store_keys(self, request: Request) -> Response:
        from firefly_weave.studio.local_store import PREFIX, StoreUnavailable

        store = self._store()
        if store is None:
            return problem(404, "WV-STUDIO-STORE-OFF", "This Studio keeps its data in the browser.")
        prefix = request.query_params.get("prefix", "")
        if PREFIX.fullmatch(prefix) is None:
            return problem(
                422, "WV-STUDIO-REQUEST", "A prefix uses letters, digits, colons, dots, underscores or hyphens"
            )
        try:
            keys = await asyncio.to_thread(store.keys, prefix)
        except StoreUnavailable:
            return problem(500, "WV-STUDIO-STORE", "Studio couldn't read what it keeps on this computer.")
        return JSONResponse({"keys": keys}, headers={"Cache-Control": "no-store"})

    @get_mapping("/studio/local/store/{key}")
    async def store_read(self, request: Request) -> Response:
        from firefly_weave.studio.local_store import KEY, StoreUnavailable

        store, key = self._store(), request.path_params["key"]
        if store is None:
            return problem(404, "WV-STUDIO-STORE-OFF", "This Studio keeps its data in the browser.")
        if KEY.fullmatch(key) is None:
            return problem(422, "WV-STUDIO-REQUEST", "Keys use letters, digits, colons, dots, underscores or hyphens")
        try:
            value = await asyncio.to_thread(store.get, key)
        except StoreUnavailable:
            return problem(500, "WV-STUDIO-STORE", "Studio couldn't read what it keeps on this computer.")
        if value is None:
            return problem(404, "WV-STUDIO-STORE-KEY", "Nothing is kept under that key.")
        return JSONResponse({"value": value}, headers={"Cache-Control": "no-store"})

    @put_mapping("/studio/local/store/{key}")
    async def store_write(self, request: Request) -> Response:
        from firefly_weave.studio.local_store import KEY, StoreFull, StoreSignedOut, StoreUnavailable

        store, key = self._store(), request.path_params["key"]
        if store is None:
            return problem(404, "WV-STUDIO-STORE-OFF", "This Studio keeps its data in the browser.")
        if KEY.fullmatch(key) is None:
            return problem(422, "WV-STUDIO-REQUEST", "Keys use letters, digits, colons, dots, underscores or hyphens")
        signed_out = problem(
            409,
            "WV-STUDIO-STORE-SIGNED-OUT",
            "You signed out of this platform; sign in again to keep its data on this computer.",
        )
        if not self.service.keeps(key):
            return signed_out
        try:
            body = StoreValue.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Send the value as text")
        try:
            # Checked again inside the store lock: a sign-out while the body arrived wins.
            refuse = lambda name: not self.service.keeps(name)  # noqa: E731
            await asyncio.to_thread(lambda: store.put(key, body.value, guarded=True, refuse=refuse))
        except StoreSignedOut:
            return signed_out
        except StoreFull:
            return problem(
                413, "WV-STUDIO-STORE-FULL", "Studio keeps at most 64 MB on this computer; remove some drafts."
            )
        except StoreUnavailable:
            return problem(500, "WV-STUDIO-STORE", "Studio couldn't keep this on this computer.")
        return Response(status_code=204)

    @delete_mapping("/studio/local/store/{key}")
    async def store_remove(self, request: Request) -> Response:
        from firefly_weave.studio.local_store import KEY, StoreUnavailable

        store, key = self._store(), request.path_params["key"]
        if store is None:
            return problem(404, "WV-STUDIO-STORE-OFF", "This Studio keeps its data in the browser.")
        if KEY.fullmatch(key) is None:
            return problem(422, "WV-STUDIO-REQUEST", "Keys use letters, digits, colons, dots, underscores or hyphens")
        try:
            await asyncio.to_thread(store.delete, key)
        except StoreUnavailable:
            return problem(500, "WV-STUDIO-STORE", "Studio couldn't remove this from this computer.")
        return Response(status_code=204)

    @post_mapping("/studio/local/testdata/check")
    async def check_test_data(self, request: Request) -> Response:
        from firefly_weave.studio.local_contracts import CheckTestDataRequest

        try:
            body = CheckTestDataRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(
                422,
                "WV-STUDIO-REQUEST",
                "Provide a bounded source, its format, its definitions and a test data document",
            )

        def work() -> Any:
            from firefly_weave.compiler.catalog import CatalogSnapshot
            from firefly_weave.compiler.parser import ParseFailure, parse_source
            from firefly_weave.compiler.test_data import check_test_data
            from firefly_weave.contracts.definitions import load_definition
            from firefly_weave.studio.local_contracts import CheckTestDataEntry, CheckTestDataResult

            try:
                document = parse_source(body.source, format=body.format).value
            except ParseFailure:
                document = None
            # A definition that doesn't load, or a second version of the same one, is left out: the
            # entries that need it are then unresolved, never accepted.
            loaded: dict[str, Any] = {}
            for value in body.definitions:
                try:
                    model = load_definition(value)
                except ValueError:
                    continue
                loaded.setdefault(f"{model.kind}:{model.metadata.name}@{model.metadata.version}", model)
            catalog = CatalogSnapshot.from_definitions(loaded.values())
            workflow = document if isinstance(document, dict) else {}
            entries = [
                CheckTestDataEntry(path=path, ok=code is None, code=code)
                for path, code in check_test_data(workflow, body.testData, catalog)
            ]
            return CheckTestDataResult(entries=entries).model_dump(mode="json", by_alias=True, exclude_none=True)

        return await local_work(work, slow="Checking this test data took too long; simplify the workflow")

    @post_mapping("/studio/local/debug/execute")
    async def debug_execute(self, request: Request) -> Response:
        from firefly_weave.studio.local_contracts import ExecuteRequest
        from firefly_weave.studio.simulation import CatalogConflict, SimulationRefused, execute

        try:
            body = ExecuteRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Provide a bounded source, its format, test data and a target")
        project = None
        if body.catalog == "project" and self.service.options.profile is not None:
            project = await self.service.project_catalog(refresh=body.refreshCatalog)
            if project is None:
                return problem(502, "WV-STUDIO-CATALOG", "Studio couldn't read the project's catalog")

        def work() -> Any:
            try:
                answer = execute(body, project)
            except SimulationRefused as refused:
                return refused
            except CatalogConflict as conflict:
                return conflict
            return answer.model_dump(mode="json", by_alias=True, exclude_unset=True)

        def respond(value: Any) -> Response:
            from firefly_weave.compiler.canonical import canonical_bytes

            if isinstance(value, SimulationRefused):
                result = {"paths": value.paths} if value.paths else None
                return problem(value.status, value.code, value.message, result)
            if isinstance(value, CatalogConflict):
                return problem(
                    409,
                    "WV-STUDIO-CATALOG-CONFLICT",
                    "A definition on this computer has the same name and version as a different one in the catalog",
                )
            return Response(canonical_bytes(value), media_type="application/json")

        return await local_work(
            work,
            pool=_DEBUG_WORK,
            slow="Executing took longer than 30 seconds; execute fewer steps",
            busy="Studio is still executing; wait for it to finish",
            respond=respond,
        )

    # --- step-through sessions for the run log -------------------------------------------------------------------

    def _debug_sessions(self) -> HostSessions:
        """The host's sessions; a new connection (sign-in, switch, sign-out) starts with none."""
        from firefly_weave.studio.debug_sessions import HostSessions

        generation = self.service.connection_generation
        kept = self._sessions
        if kept is None or kept[0] != generation:
            kept = self._sessions = (generation, HostSessions())
        return kept[1]

    async def _session_work(self, work: Callable[[], Any], status: int = 200) -> Response:
        from firefly_weave.studio.simulation import SimulationRefused

        def guarded() -> Any:
            try:
                return work()
            except SimulationRefused as refused:
                return refused

        def respond(value: Any) -> Response:
            if isinstance(value, SimulationRefused):
                return problem(value.status, value.code, value.message, {"paths": value.paths} if value.paths else None)
            if value is None:
                return Response(status_code=204)
            return Response(value.model_dump_json(by_alias=True), status_code=status, media_type="application/json")

        return await local_work(
            guarded,
            pool=_DEBUG_WORK,
            slow="The run log took longer than 30 seconds; execute fewer steps",
            busy="Studio is still executing; wait for it to finish",
            respond=respond,
        )

    @post_mapping("/studio/local/debug/sessions")
    async def debug_session_create(self, request: Request) -> Response:
        from firefly_weave.studio.local_contracts import SimulationRequest

        try:
            body = SimulationRequest.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Provide a bounded source, its format and test data")
        project = None
        if body.catalog == "project" and self.service.options.profile is not None:
            project = await self.service.project_catalog(refresh=body.refreshCatalog)
            if project is None:
                return problem(502, "WV-STUDIO-CATALOG", "Studio couldn't read the project's catalog")
        sessions = self._debug_sessions()
        return await self._session_work(lambda: sessions.create(body, project, time.monotonic()), status=201)

    @get_mapping("/studio/local/debug/sessions/{identifier}")
    async def debug_session_read(self, request: Request) -> Response:
        identifier = _session_id(request)
        if identifier is None:
            return _session_gone()
        sessions = self._debug_sessions()
        return await self._session_work(lambda: sessions.read(identifier, time.monotonic()))

    @post_mapping("/studio/local/debug/sessions/{identifier}/commands")
    async def debug_session_command(self, request: Request) -> Response:
        from firefly_weave.operations.debug.models import DebugCommand

        identifier = _session_id(request)
        if identifier is None:
            return _session_gone()
        match = request.headers.get("If-Match", "").strip().strip('"')
        if not match.isdigit():
            return problem(428, "WV-STUDIO-REQUEST", "Send the run log's revision in If-Match")
        try:
            command = DebugCommand.model_validate_json(await request.body())
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Provide a run log command")
        sessions = self._debug_sessions()
        return await self._session_work(lambda: sessions.command(identifier, int(match), command, time.monotonic()))

    @delete_mapping("/studio/local/debug/sessions/{identifier}")
    async def debug_session_delete(self, request: Request) -> Response:
        identifier = _session_id(request)
        if identifier is None:
            return _session_gone()
        sessions = self._debug_sessions()
        return await self._session_work(lambda: sessions.delete(identifier))

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

        raw = await request.body()
        if len(raw) > HTTP_ACTION_LIMIT:
            return problem(413, "WV-STUDIO-REQUEST", "HTTP request description exceeds the 1 MiB limit")
        try:
            body = HttpActionBuild.model_validate_json(raw)
        except ValidationError:
            return problem(422, "WV-STUDIO-REQUEST", "Describe the HTTP request to turn into an action")
        return await local_work(lambda: author_http_action(body.request).model_dump(by_alias=True, mode="json"))


STATIC_WAIT_SECONDS = 10


class StudioBoundary:
    """Reject rebinding/cross-site access before credentials or compiler work is reached."""

    def __init__(self, app: ASGIApp, service: StudioService) -> None:
        self.app, self.service = app, service
        self.slots = asyncio.Semaphore(8)
        self.asset_slots = asyncio.Semaphore(8)

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
        # Browser navigation and lazy chunks must not compete with slow API calls.
        static = not mutation and not request.url.path.startswith("/studio/")
        slots = self.asset_slots if static else self.slots
        if not static and slots.locked():
            await reject(429, "WV-STUDIO-BUSY", "Studio is busy; wait for the current requests")
            return
        try:
            async with asyncio.timeout(STATIC_WAIT_SECONDS):
                await slots.acquire()
        except TimeoutError:
            await reject(429, "WV-STUDIO-BUSY", "Studio is busy; wait for the current requests")
            return
        try:
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
        finally:
            slots.release()


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
