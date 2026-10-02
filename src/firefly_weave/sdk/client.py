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

"""Async typed host client. Credentials are evaluated once for every request."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from firefly_weave.compiler.api import CompileResult
from firefly_weave.compiler.catalog import CatalogLock, CatalogSnapshot
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.broker import BrokerIncident, BrokerTrigger, BrokerTriggerRequest, SourceBinding
from firefly_weave.contracts.catalog import (
    Activation,
    ActivationRequest,
    Draft,
    DraftRequest,
    PublicationRequest,
    PublishedVersion,
    RetirementRequest,
)
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionRevision, ConnectionTestResult
from firefly_weave.contracts.human_tasks import (
    AssignmentBinding,
    AssignmentBindingList,
    AssignmentBindingRequest,
    CompleteHumanTask,
    HumanTask,
    HumanTaskCommand,
    ManualControlRequest,
    ReassignHumanTask,
    TaskGroup,
    TaskGroupRequest,
)
from firefly_weave.contracts.integration_events import DeliveryAttempt, DeliveryView, Subscription, SubscriptionRequest
from firefly_weave.contracts.maintenance import RetentionApplication, RetentionPlan, RetentionRequest
from firefly_weave.contracts.operations import (
    CancelRunRequest,
    EventPage,
    HistoryExport,
    IncidentResolution,
    IncidentView,
    ReplayReport,
)
from firefly_weave.contracts.providers import ProviderReceipt, ProviderSource, ProviderSourceRequest
from firefly_weave.contracts.public import (
    ActivationExport,
    Capabilities,
    CompileResponse,
    CompilerRequest,
    Disabled,
    DraftExport,
    DraftRetirement,
    DraftView,
    Granted,
    GrantRequest,
    Identifier,
    NamedResource,
    NameRequest,
    Page,
    Problem,
    RetiredVersion,
    Revoked,
    UnavailableResource,
    VersionExport,
    VersionView,
    catalog_lock,
)
from firefly_weave.contracts.run_lifecycle import RunLifecycle, RunLifecycleRequest, RunPurgeRequest
from firefly_weave.contracts.runtime import (
    CapacityRunAcknowledgment,
    RunView,
    SignalReceipt,
    SignalRequest,
    StartRunRequest,
    UnavailableRunAcknowledgment,
)
from firefly_weave.contracts.schedules import ScheduleOccurrence, ScheduleRequest, ScheduleView
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.contracts.teams import TeamsReactivateRequest, TeamsReference, TeamsRevokeRequest
from firefly_weave.contracts.values import JsonObject
from firefly_weave.contracts.whatsapp import WhatsAppDeliveryState, WhatsAppStatusFact
from firefly_weave.contracts.workers import (
    ClaimRequest,
    CompleteRequest,
    CompletionAcknowledgment,
    CredentialGrantRequest,
    CredentialLease,
    CredentialRequest,
    FailRequest,
    InstanceRequest,
    LeaseProof,
    ReleaseRequest,
    TaskLease,
    WorkerInstance,
    WorkerRelease,
)
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugSession
from firefly_weave.sdk.errors import ContractError, PreconditionFailed, TransportError, WeaveError
from firefly_weave.triggers.models import Trigger, TriggerRequest

if TYPE_CHECKING:
    import httpx

Collection = Literal["workflows", "actions", "connectors"]


class AsyncTokenProvider(Protocol):
    async def get_access_token(self, target: str) -> str: ...


class WeaveClient:
    """Owns its async HTTP client and any supplied HTTPX transport; use async with.

    No automatic retry, redirect, proxy inheritance, or implicit request replay.
    Credentials supplied by a callable remain host-owned. Async providers receive
    the exact API origin so persisted sessions can enforce target binding.
    """

    def __init__(
        self,
        base_url: str,
        credential_provider: Callable[[], str] | AsyncTokenProvider,
        scope: Scope,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        target = urlsplit(base_url)
        if (
            target.scheme not in {"https", "http"}
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path not in {"", "/"}
        ):
            raise ValueError("An absolute API origin is required")
        if target.scheme == "http" and target.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("TLS is required outside loopback development")
        if not 0 < timeout <= 300:
            raise ValueError("A bounded request timeout is required")
        try:
            import httpx
        except ModuleNotFoundError as error:
            raise ImportError("Remote SDK requires firefly-weave[client]") from error
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self.credential_provider = credential_provider
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport, follow_redirects=False, trust_env=False
        )
        self._entered = False
        self.closed = False

    def __enter__(self) -> None:
        raise TypeError("WeaveClient is async; use 'async with' and await its operations")

    def __exit__(self, *args: object) -> None:
        pass

    async def __aenter__(self) -> WeaveClient:
        if self.closed or self._entered:
            raise RuntimeError("Client context cannot be reused")
        self._entered = True
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        self.closed = True

    async def invoke(
        self,
        operation: str,
        *,
        identifier: UUID | None = None,
        state_id: UUID | None = None,
        collection: str | None = None,
        body: BaseModel | None = None,
        revision: int | None = None,
        idempotency_key: str | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        """Typed registry boundary also used by the thin CLI; no arbitrary URL input."""
        import httpx

        if not self._entered or self.closed:
            raise RuntimeError("Use the client in an active async context")
        spec = OPERATIONS[operation]
        values = {
            "tenant": self.scope.tenant_id,
            "project": self.scope.project_id,
            "environment": self.scope.environment_id,
            "identifier": identifier,
            "state_id": state_id,
            "collection": collection,
        }
        if collection is not None and collection not in {"drafts", "workflows", "actions", "connectors"}:
            raise ValueError("Unknown catalog collection")
        import re

        if any(values[name] is None for name in re.findall(r"{([^}]+)}", spec.path)):
            raise ValueError("Operation requires its exact scope and resource identifiers")
        if body is not None and spec.request is not None:
            body = TypeAdapter(spec.request).validate_json(body.model_dump_json(by_alias=True))
        elif spec.request is not None:
            raise ValueError("Operation requires a typed request")
        elif body is not None:
            raise ValueError("Operation accepts no request body")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if revision is not None:
            if type(revision) is not int or not 1 <= revision <= 9999999999:
                raise ValueError("A positive revision is required")
            headers["If-Match"] = f'"{revision}"'
        elif spec.revision == "required":
            raise ValueError("Operation requires an expected revision")
        if idempotency_key is not None:
            if not 1 <= len(idempotency_key) <= 200 or any(ord(c) < 32 or ord(c) > 126 for c in idempotency_key):
                raise ValueError("A bounded ASCII idempotency key is required")
            headers["Idempotency-Key"] = idempotency_key
        elif spec.idempotency:
            raise ValueError("Operation requires an idempotency key")
        provider = self.credential_provider
        token = provider() if callable(provider) else await provider.get_access_token(self.base_url)
        if inspect.isawaitable(token):
            if inspect.iscoroutine(token):
                token.close()
            raise TypeError("Use AsyncTokenProvider for async credential acquisition")
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Credential provider must return an access token string")
        headers["Authorization"] = "Bearer " + token
        try:
            async with self._client.stream(
                spec.method,
                spec.canonical_path.format(**values),
                headers=headers,
                params=query,
                json=body.model_dump(mode="json", by_alias=True) if body is not None else None,
            ) as response:
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ContractError(Problem(status=502, code="WV-WIRE", message="Compressed response rejected"))
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 32 * 1024 * 1024:
                        raise ContractError(Problem(status=502, code="WV-WIRE", message="Response exceeds bound"))
                    content.extend(chunk)
                if response.headers.get("X-Weave-Wire-Version", "weave/api-v1") != "weave/api-v1":
                    raise ContractError(
                        Problem(status=502, code="WV-WIRE-VERSION", message="Unsupported API wire version")
                    )
                if response.status_code >= 300:
                    try:
                        problem = Problem.model_validate_json(content)
                        if problem.status != response.status_code:
                            raise ValueError()
                    except ValueError:
                        problem = Problem(
                            status=response.status_code if response.status_code >= 400 else 502,
                            code="WV-HTTP",
                            message="Remote request failed",
                            request_id=response.headers.get("X-Weave-Request-ID"),
                        )
                    raise (PreconditionFailed if problem.status == 412 else WeaveError)(problem)
                if response.status_code not in spec.statuses:
                    raise ValueError("Unexpected response status")
                return TypeAdapter(spec.response).validate_json(content)
        except httpx.HTTPError:
            raise TransportError(
                Problem(
                    status=503, code="WV-TRANSPORT", message="Request transport failed; mutation outcome may be unknown"
                )
            ) from None
        except (ValidationError, ValueError):
            raise ContractError(
                Problem(status=502, code="WV-WIRE", message="Response does not match the supported contract")
            ) from None

    async def compile(
        self,
        *,
        source: str | JsonObject,
        format: Literal["yaml", "json", "object"],
        catalog: CatalogSnapshot | CatalogLock | None = None,
        filename: str | None = None,
        strict: bool = False,
    ) -> CompileResult:
        return await self._compile("compiler.compile", source, format, catalog, filename, strict)

    async def validate(
        self,
        *,
        source: str | JsonObject,
        format: Literal["yaml", "json", "object"],
        catalog: CatalogSnapshot | CatalogLock | None = None,
        filename: str | None = None,
        strict: bool = False,
    ) -> CompileResult:
        return await self._compile("compiler.validate", source, format, catalog, filename, strict)

    async def _compile(
        self,
        operation: str,
        source: str | JsonObject,
        format: Literal["yaml", "json", "object"],
        catalog: CatalogSnapshot | CatalogLock | None,
        filename: str | None,
        strict: bool,
    ) -> CompileResult:
        body = CompilerRequest(
            source=source,
            format=format,
            catalog=catalog_lock(catalog) if isinstance(catalog, CatalogSnapshot) else catalog,
            filename=filename,
            strict=strict,
        )
        result = cast(CompileResponse, await self.invoke(operation, body=body))
        return result.to_result()

    async def catalog(self) -> CatalogLock:
        return cast(CatalogLock, await self.invoke("catalog.read"))

    async def capabilities(self) -> Capabilities:
        return cast(Capabilities, await self.invoke("capabilities.read"))

    async def schemas(self) -> dict[str, JsonObject]:
        return cast(dict[str, JsonObject], await self.invoke("schemas.read"))

    async def save_draft(self, identifier: UUID, document: JsonObject, *, revision: int | None = None) -> Draft:
        return cast(
            Draft,
            await self.invoke(
                "drafts.save", identifier=identifier, body=DraftRequest(document=document), revision=revision
            ),
        )

    async def delete_draft(self, identifier: UUID, *, revision: int) -> DraftRetirement:
        return cast(DraftRetirement, await self.invoke("drafts.retire", identifier=identifier, revision=revision))

    async def read_draft(self, identifier: UUID) -> DraftView:
        return cast(DraftView, await self.invoke("definitions.read", collection="drafts", identifier=identifier))

    async def export_draft(self, identifier: UUID) -> DraftExport:
        return cast(DraftExport, await self.invoke("definitions.export", collection="drafts", identifier=identifier))

    async def list_definitions(
        self, collection: Collection | Literal["drafts"], *, limit: int = 50, cursor: str | None = None
    ) -> Page[PublishedVersion | Draft | UnavailableResource]:
        return cast(
            Page[PublishedVersion | Draft | UnavailableResource],
            await self.invoke("definitions.list", collection=collection, query=self._page(limit, cursor)),
        )

    async def read_definition(self, collection: Collection, identifier: UUID) -> VersionView:
        return cast(VersionView, await self.invoke("definitions.read", collection=collection, identifier=identifier))

    async def export_definition(self, collection: Collection, identifier: UUID) -> VersionExport:
        return cast(
            VersionExport, await self.invoke("definitions.export", collection=collection, identifier=identifier)
        )

    async def publish(
        self, collection: Collection, source: str, format: Literal["yaml", "json"], *, idempotency_key: str
    ) -> PublishedVersion:
        return cast(
            PublishedVersion,
            await self.invoke(
                "definitions.publish",
                collection=collection,
                body=PublicationRequest(source=source, format=format),
                idempotency_key=idempotency_key,
            ),
        )

    async def retire_definition(
        self, collection: Collection, identifier: UUID, *, idempotency_key: str
    ) -> RetiredVersion:
        return cast(
            RetiredVersion,
            await self.invoke(
                "definitions.retire",
                collection=collection,
                identifier=identifier,
                body=RetirementRequest(),
                idempotency_key=idempotency_key,
            ),
        )

    async def activate(
        self, request: ActivationRequest, *, idempotency_key: str, revision: int | None = None
    ) -> Activation:
        return cast(
            Activation,
            await self.invoke("activations.create", body=request, revision=revision, idempotency_key=idempotency_key),
        )

    async def read_activation(self, identifier: UUID) -> Activation:
        return cast(Activation, await self.invoke("activations.read", identifier=identifier))

    async def export_activation(self, identifier: UUID) -> ActivationExport:
        return cast(ActivationExport, await self.invoke("activations.export", identifier=identifier))

    async def retire_activation(self, identifier: UUID, *, revision: int, idempotency_key: str) -> Activation:
        return cast(
            Activation,
            await self.invoke(
                "activations.retire",
                identifier=identifier,
                body=RetirementRequest(),
                revision=revision,
                idempotency_key=idempotency_key,
            ),
        )

    @staticmethod
    def _page(limit: int, cursor: str | None) -> dict[str, str | int]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page limit must be between 1 and 100")
        return {"limit": limit, **({"cursor": cursor} if cursor is not None else {})}

    async def create_connection(self, request: ConnectionRequest) -> ConnectionRevision:
        return cast(ConnectionRevision, await self.invoke("connections.create", body=request))

    async def read_connection(self, identifier: UUID) -> ConnectionRevision:
        return cast(ConnectionRevision, await self.invoke("connections.read", identifier=identifier))

    async def test_connection(self, identifier: UUID) -> ConnectionTestResult:
        return cast(
            ConnectionTestResult, await self.invoke("connections.test", identifier=identifier, body=RetirementRequest())
        )

    async def start_run(self, request: StartRunRequest, *, idempotency_key: str) -> RunView:
        return cast(RunView, await self.invoke("runs.start", body=request, idempotency_key=idempotency_key))

    async def read_run(self, identifier: UUID) -> RunView:
        return cast(RunView, await self.invoke("runs.read", identifier=identifier))

    async def signal(self, identifier: UUID, request: SignalRequest) -> SignalReceipt:
        return cast(SignalReceipt, await self.invoke("runs.signal", identifier=identifier, body=request))

    async def cancel(
        self, identifier: UUID, reason: str
    ) -> RunView | UnavailableRunAcknowledgment | CapacityRunAcknowledgment:
        return cast(
            RunView | UnavailableRunAcknowledgment | CapacityRunAcknowledgment,
            await self.invoke("runs.cancel", identifier=identifier, body=CancelRunRequest(reason=reason)),
        )

    async def retry(self, identifier: UUID, request: StartRunRequest, *, idempotency_key: str) -> RunView:
        return cast(
            RunView,
            await self.invoke("runs.retry", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )

    async def history(self, identifier: UUID, *, limit: int = 100, cursor: str | None = None) -> EventPage:
        return cast(
            EventPage, await self.invoke("runs.history", identifier=identifier, query=self._page(limit, cursor))
        )

    async def export_run(self, identifier: UUID, *, limit: int = 1000) -> HistoryExport:
        return cast(HistoryExport, await self.invoke("runs.export", identifier=identifier, query={"limit": limit}))

    async def replay(self, identifier: UUID, *, limit: int = 1000) -> ReplayReport:
        return cast(ReplayReport, await self.invoke("runs.replay", identifier=identifier, query={"limit": limit}))

    async def run_incidents(
        self,
        identifier: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[IncidentView | UnavailableResource]:
        return cast(
            Page[IncidentView | UnavailableResource],
            await self.invoke(
                "incidents.run_list",
                identifier=identifier,
                query=self._page(limit, cursor),
            ),
        )

    async def resolve_incident(self, identifier: UUID, request: IncidentResolution, *, revision: int) -> IncidentView:
        return cast(
            IncidentView, await self.invoke("incidents.resolve", identifier=identifier, body=request, revision=revision)
        )

    async def register_release(self, request: ReleaseRequest) -> WorkerRelease:
        return cast(WorkerRelease, await self.invoke("releases.create", body=request))

    async def register_worker(self, request: InstanceRequest) -> WorkerInstance:
        return cast(WorkerInstance, await self.invoke("workers.create", body=request))

    async def read_release(self, identifier: UUID) -> WorkerRelease:
        return cast(WorkerRelease, await self.invoke("releases.read", identifier=identifier))

    async def read_worker(self, identifier: UUID) -> WorkerInstance:
        return cast(WorkerInstance, await self.invoke("workers.read", identifier=identifier))

    async def revoke_worker(self, identifier: UUID) -> Revoked:
        return cast(Revoked, await self.invoke("workers.revoke", identifier=identifier))

    async def grant_connection(self, request: CredentialGrantRequest) -> Granted:
        return cast(Granted, await self.invoke("workers.grant", body=request))

    async def create_broker_trigger(self, request: BrokerTriggerRequest) -> BrokerTrigger:
        return cast(BrokerTrigger, await self.invoke("broker_triggers.create", body=request))

    async def read_broker_trigger(self, identifier: UUID) -> BrokerTrigger:
        return cast(BrokerTrigger, await self.invoke("broker_triggers.read", identifier=identifier))

    async def disable_broker_trigger(self, identifier: UUID) -> BrokerTrigger:
        return cast(BrokerTrigger, await self.invoke("broker_triggers.disable", identifier=identifier))

    async def retry_broker_trigger(self, identifier: UUID) -> BrokerTrigger:
        return cast(BrokerTrigger, await self.invoke("broker_triggers.retry", identifier=identifier))

    async def list_broker_triggers(self, *, limit: int = 50, cursor: str | None = None) -> Page[BrokerTrigger]:
        return cast(Page[BrokerTrigger], await self.invoke("broker_triggers.list", query=self._page(limit, cursor)))

    async def list_broker_incidents(self, *, limit: int = 50, cursor: str | None = None) -> Page[BrokerIncident]:
        return cast(
            Page[BrokerIncident], await self.invoke("broker_triggers.incidents", query=self._page(limit, cursor))
        )

    async def save_subscription(self, request: SubscriptionRequest) -> Subscription:
        return cast(Subscription, await self.invoke("subscriptions.save", body=request))

    async def read_subscription(self, identifier: UUID) -> Subscription:
        return cast(Subscription, await self.invoke("subscriptions.read", identifier=identifier))

    async def disable_subscription(self, identifier: UUID) -> Subscription:
        return cast(Subscription, await self.invoke("subscriptions.disable", identifier=identifier))

    async def list_subscriptions(self, *, limit: int = 50, cursor: str | None = None) -> Page[Subscription]:
        return cast(Page[Subscription], await self.invoke("subscriptions.list", query=self._page(limit, cursor)))

    async def read_delivery(self, identifier: UUID) -> DeliveryView:
        return cast(DeliveryView, await self.invoke("deliveries.read", identifier=identifier))

    async def retry_delivery(self, identifier: UUID) -> DeliveryView:
        return cast(DeliveryView, await self.invoke("deliveries.retry", identifier=identifier))

    async def list_deliveries(self, *, limit: int = 50, cursor: str | None = None) -> Page[DeliveryView]:
        return cast(Page[DeliveryView], await self.invoke("deliveries.list", query=self._page(limit, cursor)))

    async def delivery_attempts(
        self, identifier: UUID, *, limit: int = 50, cursor: str | None = None
    ) -> Page[DeliveryAttempt]:
        return cast(
            Page[DeliveryAttempt],
            await self.invoke("deliveries.history", identifier=identifier, query=self._page(limit, cursor)),
        )

    async def list_source_bindings(self, *, limit: int = 50, cursor: str | None = None) -> Page[SourceBinding]:
        return cast(Page[SourceBinding], await self.invoke("source_bindings.list", query=self._page(limit, cursor)))

    async def read_source_binding(self, identifier: UUID) -> SourceBinding:
        return cast(SourceBinding, await self.invoke("source_bindings.read", identifier=identifier))

    async def revoke_source_binding(self, identifier: UUID) -> Revoked:
        return cast(Revoked, await self.invoke("source_bindings.revoke", identifier=identifier))

    async def create_trigger(self, request: TriggerRequest) -> Trigger:
        return cast(Trigger, await self.invoke("triggers.create", body=request))

    async def read_trigger(self, identifier: UUID) -> Trigger:
        return cast(Trigger, await self.invoke("triggers.read", identifier=identifier))

    async def disable_trigger(self, identifier: UUID) -> Disabled:
        return cast(Disabled, await self.invoke("triggers.disable", identifier=identifier))

    async def save_schedule(self, request: ScheduleRequest, *, revision: int | None = None) -> ScheduleView:
        return cast(ScheduleView, await self.invoke("schedules.save", body=request, revision=revision))

    async def read_schedule(self, identifier: UUID) -> ScheduleView:
        return cast(ScheduleView, await self.invoke("schedules.read", identifier=identifier))

    async def change_schedule(
        self, identifier: UUID, action: Literal["enable", "disable", "delete"], *, revision: int
    ) -> ScheduleView:
        if action not in {"enable", "disable", "delete"}:
            raise ValueError("Unknown schedule action")
        return cast(ScheduleView, await self.invoke("schedules." + action, identifier=identifier, revision=revision))

    async def read_compatibility(self) -> CompatibilityReport:
        return cast(CompatibilityReport, await self.invoke("compatibility.read"))

    async def check_compatibility(self) -> CompatibilityReport:
        return cast(CompatibilityReport, await self.invoke("compatibility.check"))

    async def plan_retention(self, request: RetentionRequest) -> RetentionPlan:
        return cast(RetentionPlan, await self.invoke("retention.plan", body=request))

    async def read_retention_plan(self, identifier: UUID) -> RetentionPlan:
        return cast(RetentionPlan, await self.invoke("retention.read", identifier=identifier))

    async def apply_retention(self, identifier: UUID) -> RetentionApplication:
        return cast(RetentionApplication, await self.invoke("retention.apply", identifier=identifier))

    async def create_debug(self, request: DebugCreate) -> DebugSession:
        return cast(DebugSession, await self.invoke("debug.create", body=request))

    async def inspect_debug(self, identifier: UUID) -> DebugSession:
        return cast(DebugSession, await self.invoke("debug.read", identifier=identifier))

    async def command_debug(self, identifier: UUID, request: DebugCommand, *, revision: int) -> DebugSession:
        return cast(
            DebugSession, await self.invoke("debug.command", identifier=identifier, body=request, revision=revision)
        )

    async def list_activations(self, *, limit: int = 50, cursor: str | None = None) -> Page[Activation]:
        return cast(Page[Activation], await self.invoke("activations.list", query=self._page(limit, cursor)))

    async def list_connections(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ConnectionRevision | UnavailableResource]:
        return cast(
            Page[ConnectionRevision | UnavailableResource],
            await self.invoke("connections.list", query=self._page(limit, cursor)),
        )

    async def list_runs(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        business_key: str | None = None,
        correlation_key: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
    ) -> Page[RunView | UnavailableResource]:
        return cast(
            Page[RunView | UnavailableResource],
            await self.invoke(
                "runs.list",
                query={
                    **self._page(limit, cursor),
                    **{
                        key: value
                        for key, value in {
                            "business_key": business_key,
                            "correlation_key": correlation_key,
                            "status": status,
                        }.items()
                        if value is not None
                    },
                    "include_archived": str(include_archived).lower(),
                },
            ),
        )

    async def list_workers(self, *, limit: int = 50, cursor: str | None = None) -> Page[WorkerInstance]:
        return cast(Page[WorkerInstance], await self.invoke("workers.list", query=self._page(limit, cursor)))

    async def list_releases(self, *, limit: int = 50, cursor: str | None = None) -> Page[WorkerRelease]:
        return cast(Page[WorkerRelease], await self.invoke("releases.list", query=self._page(limit, cursor)))

    async def list_triggers(self, *, limit: int = 50, cursor: str | None = None) -> Page[Trigger]:
        return cast(Page[Trigger], await self.invoke("triggers.list", query=self._page(limit, cursor)))

    async def list_incidents(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> Page[IncidentView | UnavailableResource]:
        return cast(
            Page[IncidentView | UnavailableResource],
            await self.invoke("incidents.list", query=self._page(limit, cursor)),
        )

    async def list_schedules(self, *, limit: int = 50, cursor: str | None = None) -> Page[ScheduleView]:
        return cast(Page[ScheduleView], await self.invoke("schedules.list", query=self._page(limit, cursor)))

    async def schedule_history(
        self,
        identifier: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[ScheduleOccurrence]:
        return cast(
            Page[ScheduleOccurrence],
            await self.invoke(
                "schedules.history",
                identifier=identifier,
                query=self._page(limit, cursor),
            ),
        )

    async def create_tenant(self, name: str) -> Identifier:
        return cast(Identifier, await self.invoke("admin.tenant", body=NameRequest(name=name)))

    async def grant(self, request: GrantRequest) -> Identifier:
        return cast(Identifier, await self.invoke("admin.grant", body=request))

    async def create_project(self, name: str) -> Identifier:
        return cast(Identifier, await self.invoke("projects.create", body=NameRequest(name=name)))

    async def create_environment(self, name: str) -> Identifier:
        return cast(Identifier, await self.invoke("environments.create", body=NameRequest(name=name)))

    async def read_environment(self) -> NamedResource:
        return cast(NamedResource, await self.invoke("environments.read"))

    async def claim_tasks(self, request: ClaimRequest) -> list[TaskLease]:
        return cast(list[TaskLease], await self.invoke("tasks.claim", body=request))

    async def heartbeat(self, lease: LeaseProof) -> TaskLease:
        return cast(TaskLease, await self.invoke("tasks.heartbeat", body=lease))

    async def complete_task(self, request: CompleteRequest) -> CompletionAcknowledgment:
        return cast(CompletionAcknowledgment, await self.invoke("tasks.complete", body=request))

    async def fail_task(self, request: FailRequest) -> CompletionAcknowledgment:
        return cast(CompletionAcknowledgment, await self.invoke("tasks.fail", body=request))

    async def request_credentials(self, request: CredentialRequest) -> CredentialLease:
        wire = await self.invoke("tasks.credentials", body=request)
        return CredentialLease.model_validate_json(wire.model_dump_json())

    async def create_provider_source(self, request: ProviderSourceRequest) -> ProviderSource:
        return cast(ProviderSource, await self.invoke("provider_sources.create", body=request))

    async def read_provider_source(self, identifier: UUID) -> ProviderSource:
        return cast(ProviderSource, await self.invoke("provider_sources.read", identifier=identifier))

    async def disable_provider_source(self, identifier: UUID) -> ProviderSource:
        return cast(ProviderSource, await self.invoke("provider_sources.disable", identifier=identifier))

    async def list_provider_sources(self, *, limit: int = 50, cursor: str | None = None) -> Page[ProviderSource]:
        return cast(Page[ProviderSource], await self.invoke("provider_sources.list", query=self._page(limit, cursor)))

    async def read_provider_receipt(self, identifier: UUID) -> ProviderReceipt:
        return cast(ProviderReceipt, await self.invoke("provider_receipts.read", identifier=identifier))

    async def list_provider_receipts(self, *, limit: int = 50, cursor: str | None = None) -> Page[ProviderReceipt]:
        return cast(Page[ProviderReceipt], await self.invoke("provider_receipts.list", query=self._page(limit, cursor)))

    async def retry_provider_receipt(self, identifier: UUID) -> ProviderReceipt:
        return cast(ProviderReceipt, await self.invoke("provider_receipts.retry", identifier=identifier))

    async def read_teams_reference(self, identifier: UUID) -> TeamsReference:
        return cast(TeamsReference, await self.invoke("teams_references.read", identifier=identifier))

    async def list_teams_references(self, *, limit: int = 50, cursor: str | None = None) -> Page[TeamsReference]:
        return cast(Page[TeamsReference], await self.invoke("teams_references.list", query=self._page(limit, cursor)))

    async def revoke_teams_reference(self, identifier: UUID, request: TeamsRevokeRequest) -> TeamsReference:
        return cast(TeamsReference, await self.invoke("teams_references.revoke", identifier=identifier, body=request))

    async def reactivate_teams_reference(self, identifier: UUID, request: TeamsReactivateRequest) -> TeamsReference:
        return cast(
            TeamsReference, await self.invoke("teams_references.reactivate", identifier=identifier, body=request)
        )

    async def whatsapp_delivery_state(self, source_id: UUID, message_id: str) -> WhatsAppDeliveryState:
        return cast(
            WhatsAppDeliveryState,
            await self.invoke("whatsapp_statuses.read", identifier=source_id, query={"message_id": message_id}),
        )

    async def list_whatsapp_status_facts(
        self, source_id: UUID, state_id: UUID, *, limit: int = 50, cursor: str | None = None
    ) -> Page[WhatsAppStatusFact]:
        return cast(
            Page[WhatsAppStatusFact],
            await self.invoke(
                "whatsapp_statuses.facts", identifier=source_id, state_id=state_id, query=self._page(limit, cursor)
            ),
        )

    async def human_tasks(
        self, *, status: str | None = None, limit: int = 50, cursor: str | None = None
    ) -> Page[HumanTask]:
        query = self._page(limit, cursor)
        if status is not None:
            query["status"] = status
        return cast(Page[HumanTask], await self.invoke("human_tasks.list", query=query))

    async def read_human_task(self, identifier: UUID) -> HumanTask:
        return cast(HumanTask, await self.invoke("human_tasks.read", identifier=identifier))

    async def claim_human_task(self, identifier: UUID, *, revision: int, idempotency_key: str) -> HumanTask:
        return cast(
            HumanTask,
            await self.invoke(
                "human_tasks.claim",
                identifier=identifier,
                body=HumanTaskCommand(expected_revision=revision),
                idempotency_key=idempotency_key,
            ),
        )

    async def release_human_task(self, identifier: UUID, *, revision: int, idempotency_key: str) -> HumanTask:
        return cast(
            HumanTask,
            await self.invoke(
                "human_tasks.release",
                identifier=identifier,
                body=HumanTaskCommand(expected_revision=revision),
                idempotency_key=idempotency_key,
            ),
        )

    async def complete_human_task(
        self, identifier: UUID, request: CompleteHumanTask, *, idempotency_key: str
    ) -> HumanTask:
        return cast(
            HumanTask,
            await self.invoke(
                "human_tasks.complete", identifier=identifier, body=request, idempotency_key=idempotency_key
            ),
        )

    async def reassign_human_task(
        self, identifier: UUID, request: ReassignHumanTask, *, idempotency_key: str
    ) -> HumanTask:
        return cast(
            HumanTask,
            await self.invoke(
                "human_tasks.reassign", identifier=identifier, body=request, idempotency_key=idempotency_key
            ),
        )

    async def assignment_bindings(self) -> AssignmentBindingList:
        return cast(AssignmentBindingList, await self.invoke("human_assignments.list"))

    async def put_assignment_binding(
        self, request: AssignmentBindingRequest, *, idempotency_key: str
    ) -> AssignmentBinding:
        return cast(
            AssignmentBinding, await self.invoke("human_assignments.put", body=request, idempotency_key=idempotency_key)
        )

    async def put_human_group(self, request: TaskGroupRequest, *, idempotency_key: str) -> TaskGroup:
        return cast(TaskGroup, await self.invoke("human_groups.put", body=request, idempotency_key=idempotency_key))

    async def pause_run(self, identifier: UUID, request: ManualControlRequest, *, idempotency_key: str) -> RunView:
        return cast(
            RunView,
            await self.invoke("runs.pause", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )

    async def run_lifecycle(self, identifier: UUID) -> RunLifecycle:
        return cast(RunLifecycle, await self.invoke("runs.lifecycle", identifier=identifier))

    async def archive_run(
        self, identifier: UUID, request: RunLifecycleRequest, *, idempotency_key: str
    ) -> RunLifecycle:
        return cast(
            RunLifecycle,
            await self.invoke("runs.archive", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )

    async def restore_run(
        self, identifier: UUID, request: RunLifecycleRequest, *, idempotency_key: str
    ) -> RunLifecycle:
        return cast(
            RunLifecycle,
            await self.invoke("runs.restore", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )

    async def purge_run(self, identifier: UUID, request: RunPurgeRequest, *, idempotency_key: str) -> RunLifecycle:
        return cast(
            RunLifecycle,
            await self.invoke("runs.purge", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )

    async def resume_run(self, identifier: UUID, request: ManualControlRequest, *, idempotency_key: str) -> RunView:
        return cast(
            RunView,
            await self.invoke("runs.resume", identifier=identifier, body=request, idempotency_key=idempotency_key),
        )
