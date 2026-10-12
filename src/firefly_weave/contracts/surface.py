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

"""Product operation identities and exact wire DTOs; native PyFly owns OpenAPI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, Field

from firefly_weave.compiler.catalog import CatalogLock
from firefly_weave.contracts.ai import (
    AIConnectionTestRequest,
    AIConnectionTestResult,
    AIEndpointsResult,
    AIModelsQuery,
    AIModelsResult,
    AIReadinessResult,
    AISetupGrantRequest,
    AISetupGrantResult,
    AISetupPublishResult,
)
from firefly_weave.contracts.bindable_connections import BindableConnection, BindableConnectionQuery
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
from firefly_weave.contracts.client_configuration import ClientConfiguration
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.connector_descriptors import AdapterName, ConnectorDescriptorView
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionRevision, ConnectionTestResult
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    Deployment,
    DeploymentJob,
    DeploymentLease,
    DeploymentLeaseProof,
    DeploymentObservation,
    DeploymentPlan,
    DeploymentRequest,
    DeploymentRunner,
    DeploymentTarget,
    JobCancelRequest,
    ObserveRequest,
    PlanApproval,
    PlanApprovalRequest,
    PlanRequest,
    ReconcileJobRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.contracts.email import (
    EmailConversation,
    EmailConversationDetail,
    EmailCorrelationRequest,
    EmailCorrelationToken,
    EmailReceipt,
    EmailReplyRequest,
    EmailSendRequest,
    EmailSourceRequest,
    EmailSourceResult,
    EmailSourceStatus,
    EmailSubmission,
    EmailTokenRequest,
)
from firefly_weave.contracts.file_workers import WorkerFileAccess, WorkerFileChunk, WorkerFileCreate, WorkerFileRead
from firefly_weave.contracts.files import FileChunk, FileChunkRead, FileCommand, FileCreate, FileUpload
from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
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
from firefly_weave.contracts.identity import IdentityView
from firefly_weave.contracts.integration_events import DeliveryAttempt, DeliveryView, Subscription, SubscriptionRequest
from firefly_weave.contracts.language import LanguageManifest
from firefly_weave.contracts.lumi import (
    LumiAskRequest,
    LumiConfiguration,
    LumiConfigurationRequest,
    LumiReply,
    LumiStatus,
)
from firefly_weave.contracts.maintenance import RetentionApplication, RetentionPlan, RetentionRequest
from firefly_weave.contracts.members import (
    MemberBinding,
    MemberGrantRequest,
    PrincipalCreateRequest,
    PrincipalIdentityRequest,
    PrincipalIdentityResult,
    PrincipalRecord,
    PrincipalStatusRequest,
)
from firefly_weave.contracts.operations import (
    CancelRunRequest,
    EventPage,
    HistoryExport,
    IncidentResolution,
    IncidentView,
    ReplayReport,
)
from firefly_weave.contracts.providers import (
    ProviderIngressResponse,
    ProviderReceipt,
    ProviderSource,
    ProviderSourceRequest,
)
from firefly_weave.contracts.public import (
    ActivationExport,
    Capabilities,
    CompileResponse,
    CompilerRequest,
    DecisionEvaluation,
    DecisionEvaluationRequest,
    Disabled,
    DraftExport,
    DraftRetirement,
    DraftView,
    Granted,
    GrantRequest,
    Health,
    Identifier,
    NamedResource,
    NameRequest,
    Page,
    Problem,
    RetiredVersion,
    Revoked,
    UnavailableResource,
    UnsupportedResource,
    VersionExport,
    VersionView,
    WebhookEnvelope,
)
from firefly_weave.contracts.run_lifecycle import RunLifecycle, RunLifecycleRequest, RunPurgeRequest
from firefly_weave.contracts.run_views import (
    RunLogPage,
    RunLogQuery,
    RunStepQuery,
    RunSummaryPage,
    RunSummaryQuery,
    StepFactPage,
)
from firefly_weave.contracts.runtime import (
    CapacityRunAcknowledgment,
    RunView,
    SignalReceipt,
    SignalRequest,
    StartRunRequest,
    UnavailableRunAcknowledgment,
)
from firefly_weave.contracts.schedules import ScheduleOccurrence, ScheduleRequest, ScheduleView
from firefly_weave.contracts.teams import TeamsReactivateRequest, TeamsReference, TeamsRevokeRequest
from firefly_weave.contracts.values import JsonObjectData
from firefly_weave.contracts.whatsapp import MessageId, WhatsAppDeliveryState, WhatsAppStatusFact
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
    TaskExecutionContext,
    TaskLease,
    WorkerControlRequest,
    WorkerInstance,
    WorkerRelease,
    WorkerStatus,
)
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugSession
from firefly_weave.triggers.models import Trigger, TriggerReceipt, TriggerRequest

if TYPE_CHECKING:
    from pyfly.web import OpenAPIOperation

PROJECT = "/tenants/{tenant}/projects/{project}"
ENVIRONMENT = PROJECT + "/environments/{environment}"
type RevisionTag = Annotated[str, Field(pattern=r'^(?:"[1-9][0-9]{0,9}"|[1-9][0-9]{0,9})$')]
type CanonicalRevisionTag = Annotated[str, Field(pattern=r'^"[1-9][0-9]{0,9}"$')]
type PageLimit = Annotated[int, Field(ge=1, le=100)]


def _present(annotation: Any) -> Any:
    """A query parameter is absent rather than null: document only its non-null type."""
    choices = [item for item in get_args(annotation) if item is not NoneType]
    return choices[0] if get_origin(annotation) in (Union, UnionType) and len(choices) == 1 else annotation


class CredentialResponse(CredentialLease):
    # This one authorized lease-scoped endpoint deliberately serializes the value.
    value: str = Field(repr=False)


@dataclass(frozen=True)
class Operation:
    id: str
    path: str
    method: str
    response: Any
    capability: str
    request: Any = None
    statuses: tuple[int, ...] = (200,)
    page: bool = False
    revision: Literal["none", "optional", "required"] = "none"
    idempotency: bool = False
    # Documents an accepted but optional Idempotency-Key; ``idempotency`` makes it required.
    optional_idempotency: bool = False
    etag: bool = False
    request_required: bool = True
    # Public operations are documented without bearer security; AuthenticationFilter
    # must allowlist exactly the same routes and methods.
    public: bool = False
    # Shown instead of the capability sentence for public operations without one.
    description: str = ""
    # Strict query model: OpenAPI documents its fields and the controller decodes the same model.
    query: type[BaseModel] | None = None
    # A published contract this server does not serve yet: authorized reads answer 501 WV-UNAVAILABLE.
    served: bool = True

    def __post_init__(self) -> None:
        if not self.capability and not (self.public and self.description):
            raise ValueError("An operation without a capability must be public and described")

    @property
    def canonical_path(self) -> str:
        return "/api/v1" + self.path if self.path.startswith("/tenants/") else self.path

    def native(self) -> OpenAPIOperation:
        from pyfly.web import OpenAPIHeader, OpenAPIOperation, OpenAPIParameter, OpenAPIRequestBody, OpenAPIResponse

        params = [
            OpenAPIParameter(
                name,
                "path",
                Literal["workflows", "actions", "connectors", "decision-tables", "drafts"]
                if name == "collection" and self.method == "GET"
                else Literal["workflows", "actions", "connectors", "decision-tables"]
                if name == "collection"
                else AdapterName
                if name == "adapter"
                else UUID,
            )
            for name in re.findall(r"{([^}]+)}", self.path)
        ]
        if self.query is not None:
            for name, field in self.query.model_fields.items():
                documented = (
                    {}
                    if field.is_required() or field.default is None or field.default_factory is not None
                    else {"default": field.default}
                )
                params.append(
                    OpenAPIParameter(
                        name, "query", _present(field.annotation), required=field.is_required(), **documented
                    )
                )
        if self.id == "runs.list":
            params += [
                OpenAPIParameter("business_key", "query", Annotated[str, Field(max_length=200)], required=False),
                OpenAPIParameter("correlation_key", "query", Annotated[str, Field(max_length=200)], required=False),
                OpenAPIParameter(
                    "status",
                    "query",
                    Literal[
                        "queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"
                    ],
                    required=False,
                ),
                OpenAPIParameter("include_archived", "query", bool, required=False, default=False),
            ]
        if self.id == "human_tasks.list":
            params.append(
                OpenAPIParameter(
                    "status", "query", Literal["ready", "claimed", "completed", "expired", "cancelled"], required=False
                )
            )
        if self.id == "email_conversations.read":
            params += [
                OpenAPIParameter("limit", "query", PageLimit, required=False, default=50),
                OpenAPIParameter("cursor", "query", str, required=False),
            ]
        if self.id == "whatsapp_statuses.read":
            params.append(OpenAPIParameter("message_id", "query", MessageId))
        if self.page and self.id.startswith(
            (
                "deployment_targets.",
                "deployments.",
                "deployment_observations.",
                "deployment_plans.",
                "deployment_jobs.",
                "deployment_runners.",
            )
        ):
            params.append(OpenAPIParameter("target_id", "query", UUID, required=False))
            if self.id == "deployment_plans.list":
                params.append(OpenAPIParameter("deployment_id", "query", UUID, required=False))
        if self.page:
            params += [
                OpenAPIParameter("limit", "query", PageLimit, required=False, default=50),
                OpenAPIParameter("cursor", "query", str, required=False),
            ]
        if self.id in {"runs.history", "runs.export", "runs.replay"}:
            params += [
                OpenAPIParameter(
                    "limit",
                    "query",
                    Annotated[int, Field(ge=1, le=100 if self.id == "runs.history" else 1000)],
                    required=False,
                    default=100 if self.id == "runs.history" else 1000,
                )
            ]
            if self.id == "runs.history":
                params.append(OpenAPIParameter("cursor", "query", str, required=False))
        if self.revision != "none":
            params.append(OpenAPIParameter("If-Match", "header", RevisionTag, required=self.revision == "required"))
        if self.idempotency or self.optional_idempotency:
            params.append(
                OpenAPIParameter(
                    "Idempotency-Key",
                    "header",
                    Annotated[str, Field(min_length=1, max_length=200)],
                    required=self.idempotency,
                )
            )
        headers = {
            "X-Weave-Request-ID": OpenAPIHeader(str, required=True),
            "X-Weave-Wire-Version": OpenAPIHeader(Literal["weave/api-v1"], required=True),
        }
        response_headers = {**headers, **({"ETag": OpenAPIHeader(CanonicalRevisionTag)} if self.etag else {})}
        if self.id == "tasks.credentials":
            response_headers.update(
                {
                    "Cache-Control": OpenAPIHeader(Literal["no-store"], required=True),
                    "Pragma": OpenAPIHeader(Literal["no-cache"], required=True),
                }
            )
        responses: dict[int | str, OpenAPIResponse] = {
            status: OpenAPIResponse("Success", {"application/json": self.response}, response_headers)
            for status in self.statuses
        }
        if self.id.startswith("provider_ingress."):
            responses = {
                status: OpenAPIResponse(
                    "Authenticated provider ACK after durable admission; provider-specific body",
                    {"application/json": Any, "text/plain": str},
                    response_headers,
                )
                for status in (200, 201, 202, 204, "2XX")
            }
        if self.id == "health.ready":
            responses[503] = OpenAPIResponse("Unavailable", {"application/problem+json": Problem}, headers)
        if self.capability:
            responses.update(
                {
                    status: OpenAPIResponse(
                        "Problem",
                        {"application/problem+json": Problem},
                        {
                            **headers,
                            **(
                                {"WWW-Authenticate": OpenAPIHeader(Literal["Bearer"])}
                                if status == 401 and not self.public
                                else {}
                            ),
                        },
                    )
                    for status in (401, 403, 404, 409, 412, 413, 422, 500)
                }
            )
        if not self.served:
            responses[501] = OpenAPIResponse(
                "Not served by this server yet", {"application/problem+json": Problem}, headers
            )
        if self.id.startswith("debug."):
            responses[410] = OpenAPIResponse("Expired session", {"application/problem+json": Problem}, headers)
        security: list[dict[str, list[str]]] = (
            [] if self.public else [{"webhookSignature": []}] if self.id == "webhooks.receive" else [{"bearer": []}]
        )
        if self.id == "webhooks.receive":
            params += [
                OpenAPIParameter(name, "header", str, required=name != "X-Weave-Event-ID")
                for name in ("X-Weave-Signature", "X-Weave-Timestamp", "X-Weave-Event-ID")
            ]
        return OpenAPIOperation(
            operation_id=self.id,
            summary=self.id.replace(".", " "),
            description=(
                "Required capability: "
                + self.capability
                + ". Current local scoped grants are authoritative."
                + (
                    " Teams must be explicitly enabled; otherwise these routes return unavailable (409)."
                    if self.id.startswith("teams_references.")
                    else ""
                )
                + (
                    ""
                    if self.served
                    else " Published contract: this server answers 501 WV-UNAVAILABLE until it serves it."
                )
                if self.capability
                else self.description
            ),
            tags=[self.id.split(".")[0]],
            parameters=params,
            request_body=(
                OpenAPIRequestBody({"application/octet-stream": bytes, "application/json": bytes})
                if self.id == "provider_ingress.receive"
                else OpenAPIRequestBody({"application/json": self.request}, required=self.request_required)
                if self.request is not None
                else None
            ),
            responses=responses,
            replace_responses=True,
            security=security,
        )


OPERATIONS = {
    item.id: item
    for item in (
        Operation(
            "deployment_plans.approval",
            ENVIRONMENT + "/deployment-plans/{identifier}/approval",
            "GET",
            PlanApproval | None,
            "deployment.read",
        ),
        Operation(
            "deployment_jobs.reconcile",
            ENVIRONMENT + "/deployment-jobs/{identifier}/reconcile",
            "POST",
            DeploymentJob,
            "deployment.apply and deployment.approve",
            ReconcileJobRequest,
        ),
        Operation(
            "deployment_targets.list",
            ENVIRONMENT + "/deployment-targets",
            "GET",
            Page[DeploymentTarget],
            "deployment.read",
            page=True,
        ),
        Operation(
            "deployment_targets.read",
            ENVIRONMENT + "/deployment-targets/{identifier}",
            "GET",
            DeploymentTarget,
            "deployment.read",
        ),
        Operation(
            "deployments.list", ENVIRONMENT + "/deployments", "GET", Page[Deployment], "deployment.read", page=True
        ),
        Operation("deployments.read", ENVIRONMENT + "/deployments/{identifier}", "GET", Deployment, "deployment.read"),
        Operation(
            "deployment_observations.list",
            ENVIRONMENT + "/deployment-observations",
            "GET",
            Page[DeploymentObservation],
            "deployment.read",
            page=True,
        ),
        Operation(
            "deployment_observations.read",
            ENVIRONMENT + "/deployment-observations/{identifier}",
            "GET",
            DeploymentObservation,
            "deployment.read",
        ),
        Operation(
            "deployment_plans.list",
            ENVIRONMENT + "/deployment-plans",
            "GET",
            Page[DeploymentPlan],
            "deployment.read",
            page=True,
        ),
        Operation(
            "deployment_plans.read",
            ENVIRONMENT + "/deployment-plans/{identifier}",
            "GET",
            DeploymentPlan,
            "deployment.read",
        ),
        Operation(
            "deployment_jobs.list",
            ENVIRONMENT + "/deployment-jobs",
            "GET",
            Page[DeploymentJob],
            "deployment.read",
            page=True,
        ),
        Operation(
            "deployment_jobs.read",
            ENVIRONMENT + "/deployment-jobs/{identifier}",
            "GET",
            DeploymentJob,
            "deployment.read",
        ),
        Operation(
            "deployment_runners.list",
            ENVIRONMENT + "/deployment-runners",
            "GET",
            Page[DeploymentRunner],
            "deployment.read",
            page=True,
        ),
        Operation(
            "deployment_runners.read",
            ENVIRONMENT + "/deployment-runners/{identifier}",
            "GET",
            DeploymentRunner,
            "deployment.read",
        ),
        Operation(
            "deployment_targets.create",
            ENVIRONMENT + "/deployment-targets",
            "POST",
            DeploymentTarget,
            "target.manage",
            TargetRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "deployments.create",
            ENVIRONMENT + "/deployments",
            "POST",
            Deployment,
            "target.manage",
            DeploymentRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "deployment_observations.create",
            ENVIRONMENT + "/deployment-observations",
            "POST",
            DeploymentJob,
            "deployment.plan",
            ObserveRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "deployment_plans.create",
            ENVIRONMENT + "/deployment-plans",
            "POST",
            DeploymentPlan,
            "deployment.plan",
            PlanRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "deployment_targets.update",
            ENVIRONMENT + "/deployment-targets/{identifier}",
            "PUT",
            DeploymentTarget,
            "target.manage",
            TargetUpdate,
            revision="required",
            etag=True,
        ),
        Operation(
            "deployments.update",
            ENVIRONMENT + "/deployments/{identifier}",
            "PUT",
            Deployment,
            "target.manage",
            DeploymentRequest,
            revision="required",
            etag=True,
        ),
        Operation(
            "deployment_plans.approve",
            ENVIRONMENT + "/deployment-plans/{identifier}/approve",
            "POST",
            PlanApproval,
            "deployment.approve",
            PlanApprovalRequest,
        ),
        Operation(
            "deployment_plans.apply",
            ENVIRONMENT + "/deployment-plans/{identifier}/apply",
            "POST",
            DeploymentJob,
            "deployment.apply",
            ApplyPlanRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "deployment_jobs.cancel",
            ENVIRONMENT + "/deployment-jobs/{identifier}/cancel",
            "POST",
            DeploymentJob,
            "deployment.cancel",
            JobCancelRequest,
        ),
        Operation(
            "deployment_runners.create",
            ENVIRONMENT + "/deployment-runners",
            "POST",
            DeploymentRunner,
            "runner.register",
            RunnerRegistration,
            (201,),
        ),
        Operation(
            "deployment_runners.claim",
            ENVIRONMENT + "/deployment-runners/claim",
            "POST",
            DeploymentLease | None,
            "runner.claim",
            RunnerClaimRequest,
        ),
        Operation(
            "deployment_runners.renew",
            ENVIRONMENT + "/deployment-runners/renew",
            "POST",
            DeploymentLease,
            "runner.renew",
            DeploymentLeaseProof,
        ),
        Operation(
            "deployment_runners.report",
            ENVIRONMENT + "/deployment-runners/report",
            "POST",
            DeploymentJob,
            "runner.report",
            RunnerReport,
        ),
        Operation(
            "deployment_runners.revoke",
            ENVIRONMENT + "/deployment-runners/{identifier}/revoke",
            "POST",
            DeploymentRunner,
            "target.manage",
        ),
        Operation(
            "human_files.create",
            ENVIRONMENT + "/human-tasks/{identifier}/files/create",
            "POST",
            FileUpload,
            "human_task.complete",
            request=HumanFileCreate,
            idempotency=True,
        ),
        Operation(
            "human_files.chunk",
            ENVIRONMENT + "/human-tasks/{identifier}/files/chunk",
            "POST",
            FileUpload,
            "human_task.complete",
            request=HumanFileChunk,
        ),
        Operation(
            "human_files.finish",
            ENVIRONMENT + "/human-tasks/{identifier}/files/finish",
            "POST",
            FileUpload,
            "human_task.complete",
            request=HumanFileCommand,
        ),
        Operation(
            "human_files.read",
            ENVIRONMENT + "/human-tasks/{identifier}/files/read",
            "POST",
            FileUpload,
            "human_task.read",
            request=HumanFileCommand,
        ),
        Operation(
            "human_files.download",
            ENVIRONMENT + "/human-tasks/{identifier}/files/download",
            "POST",
            FileChunk,
            "human_task.read",
            request=HumanFileRead,
        ),
        Operation(
            "files.create",
            ENVIRONMENT + "/files",
            "POST",
            FileUpload,
            "file.manage",
            FileCreate,
            (201,),
            idempotency=True,
        ),
        Operation("files.list", ENVIRONMENT + "/files", "GET", Page[FileUpload], "file.read", page=True),
        Operation("files.read", ENVIRONMENT + "/files/{identifier}", "GET", FileUpload, "file.read"),
        Operation(
            "files.chunk", ENVIRONMENT + "/files/{identifier}/chunks", "POST", FileUpload, "file.manage", FileChunk
        ),
        Operation(
            "files.finish", ENVIRONMENT + "/files/{identifier}/finish", "POST", FileUpload, "file.manage", FileCommand
        ),
        Operation(
            "files.download",
            ENVIRONMENT + "/files/{identifier}/download",
            "POST",
            FileChunk,
            "file.read",
            FileChunkRead,
        ),
        Operation("files.delete", ENVIRONMENT + "/files/{identifier}", "DELETE", Revoked, "file.manage"),
        Operation(
            "task_files.create",
            ENVIRONMENT + "/tasks/files/create",
            "POST",
            FileUpload,
            "task.claim",
            WorkerFileCreate,
            (201,),
        ),
        Operation(
            "task_files.chunk", ENVIRONMENT + "/tasks/files/chunk", "POST", FileUpload, "task.claim", WorkerFileChunk
        ),
        Operation(
            "task_files.finish", ENVIRONMENT + "/tasks/files/finish", "POST", FileUpload, "task.claim", WorkerFileAccess
        ),
        Operation(
            "task_files.read", ENVIRONMENT + "/tasks/files/read", "POST", FileUpload, "task.claim", WorkerFileAccess
        ),
        Operation(
            "task_files.download",
            ENVIRONMENT + "/tasks/files/download",
            "POST",
            FileChunk,
            "task.claim",
            WorkerFileRead,
        ),
        Operation(
            "lumi.configuration.read", ENVIRONMENT + "/lumi/configuration", "GET", LumiConfiguration, "lumi.manage"
        ),
        Operation(
            "lumi.configuration.write",
            ENVIRONMENT + "/lumi/configuration",
            "PUT",
            LumiConfiguration,
            "lumi.manage",
            request=LumiConfigurationRequest,
            revision="optional",
        ),
        Operation("lumi.status", ENVIRONMENT + "/lumi/status", "GET", LumiStatus, "lumi.use"),
        Operation("lumi.ask", ENVIRONMENT + "/lumi/ask", "POST", LumiReply, "lumi.use", request=LumiAskRequest),
        Operation(
            "compatibility.read",
            PROJECT + "/operations/compatibility",
            "GET",
            CompatibilityReport,
            "status.read",
        ),
        Operation(
            "compatibility.check",
            PROJECT + "/operations/compatibility/check",
            "POST",
            CompatibilityReport,
            "compatibility.check",
        ),
        Operation(
            "retention.plan",
            PROJECT + "/operations/retention/plans",
            "POST",
            RetentionPlan,
            "retention.plan",
            RetentionRequest,
            statuses=(201,),
        ),
        Operation(
            "retention.read",
            PROJECT + "/operations/retention/plans/{identifier}",
            "GET",
            RetentionPlan,
            "retention.plan",
        ),
        Operation(
            "retention.apply",
            PROJECT + "/operations/retention/plans/{identifier}/apply",
            "POST",
            RetentionApplication,
            "retention.apply",
        ),
        Operation(
            "teams_references.read",
            ENVIRONMENT + "/teams-references/{identifier}",
            "GET",
            TeamsReference,
            "trigger.manage",
        ),
        Operation(
            "teams_references.list",
            ENVIRONMENT + "/teams-references",
            "GET",
            Page[TeamsReference],
            "trigger.manage",
            page=True,
        ),
        Operation(
            "teams_references.revoke",
            ENVIRONMENT + "/teams-references/{identifier}/revoke",
            "POST",
            TeamsReference,
            "trigger.manage + connection.manage + connection.bind",
            TeamsRevokeRequest,
        ),
        Operation(
            "teams_references.reactivate",
            ENVIRONMENT + "/teams-references/{identifier}/reactivate",
            "POST",
            TeamsReference,
            "trigger.manage + connection.manage + connection.bind",
            TeamsReactivateRequest,
        ),
        Operation(
            "whatsapp_statuses.read",
            ENVIRONMENT + "/provider-sources/{identifier}/whatsapp-statuses",
            "GET",
            WhatsAppDeliveryState,
            "run.read",
        ),
        Operation(
            "whatsapp_statuses.facts",
            ENVIRONMENT + "/provider-sources/{identifier}/whatsapp-statuses/{state_id}/facts",
            "GET",
            Page[WhatsAppStatusFact],
            "run.read",
            page=True,
        ),
        Operation(
            "provider_sources.create",
            ENVIRONMENT + "/provider-sources",
            "POST",
            ProviderSource,
            "trigger.manage + connection.manage + connection.bind + target authority",
            ProviderSourceRequest,
            (201,),
        ),
        Operation(
            "provider_sources.read", ENVIRONMENT + "/provider-sources/{identifier}", "GET", ProviderSource, "run.read"
        ),
        Operation(
            "provider_sources.list",
            ENVIRONMENT + "/provider-sources",
            "GET",
            Page[ProviderSource],
            "run.read",
            page=True,
        ),
        Operation(
            "provider_sources.disable",
            ENVIRONMENT + "/provider-sources/{identifier}/disable",
            "POST",
            ProviderSource,
            "trigger.manage + connection.bind + target authority",
        ),
        Operation(
            "provider_receipts.read",
            ENVIRONMENT + "/provider-receipts/{identifier}",
            "GET",
            ProviderReceipt,
            "run.read",
        ),
        Operation(
            "provider_receipts.list",
            ENVIRONMENT + "/provider-receipts",
            "GET",
            Page[ProviderReceipt],
            "run.read",
            page=True,
        ),
        Operation(
            "provider_receipts.retry",
            ENVIRONMENT + "/provider-receipts/{identifier}/retry",
            "POST",
            ProviderReceipt,
            "run.retry + current source/target authority",
        ),
        Operation(
            "provider_ingress.receive",
            "/provider-ingress/{identifier}",
            "POST",
            ProviderIngressResponse,
            "provider verification",
            public=True,
        ),
        Operation(
            "provider_ingress.challenge",
            "/provider-ingress/{identifier}",
            "GET",
            ProviderIngressResponse,
            "provider challenge verification",
            public=True,
        ),
        Operation(
            "human_tasks.list", ENVIRONMENT + "/human-tasks", "GET", Page[HumanTask], "human_task.read", page=True
        ),
        Operation("human_tasks.read", ENVIRONMENT + "/human-tasks/{identifier}", "GET", HumanTask, "human_task.read"),
        Operation(
            "human_tasks.claim",
            ENVIRONMENT + "/human-tasks/{identifier}/claim",
            "POST",
            HumanTask,
            "human_task.claim",
            HumanTaskCommand,
            idempotency=True,
        ),
        Operation(
            "human_tasks.release",
            ENVIRONMENT + "/human-tasks/{identifier}/release",
            "POST",
            HumanTask,
            "human_task.release",
            HumanTaskCommand,
            idempotency=True,
        ),
        Operation(
            "human_tasks.reassign",
            ENVIRONMENT + "/human-tasks/{identifier}/reassign",
            "POST",
            HumanTask,
            "human_task.manage",
            ReassignHumanTask,
            idempotency=True,
        ),
        Operation(
            "human_tasks.complete",
            ENVIRONMENT + "/human-tasks/{identifier}/complete",
            "POST",
            HumanTask,
            "human_task.complete",
            CompleteHumanTask,
            idempotency=True,
        ),
        Operation(
            "human_assignments.list",
            ENVIRONMENT + "/human-assignments",
            "GET",
            AssignmentBindingList,
            "assignment.read",
        ),
        Operation(
            "human_assignments.put",
            ENVIRONMENT + "/human-assignments",
            "POST",
            AssignmentBinding,
            "assignment.manage",
            AssignmentBindingRequest,
            idempotency=True,
        ),
        Operation(
            "human_groups.put",
            ENVIRONMENT + "/human-groups",
            "POST",
            TaskGroup,
            "assignment.manage",
            TaskGroupRequest,
            idempotency=True,
        ),
        Operation("runs.lifecycle", ENVIRONMENT + "/runs/{identifier}/lifecycle", "GET", RunLifecycle, "run.read"),
        Operation(
            "runs.archive",
            ENVIRONMENT + "/runs/{identifier}/archive",
            "POST",
            RunLifecycle,
            "run.archive",
            RunLifecycleRequest,
            idempotency=True,
        ),
        Operation(
            "runs.restore",
            ENVIRONMENT + "/runs/{identifier}/restore",
            "POST",
            RunLifecycle,
            "run.archive",
            RunLifecycleRequest,
            idempotency=True,
        ),
        Operation(
            "runs.purge",
            ENVIRONMENT + "/runs/{identifier}/purge",
            "POST",
            RunLifecycle,
            "run.purge",
            RunPurgeRequest,
            idempotency=True,
        ),
        Operation(
            "runs.pause",
            ENVIRONMENT + "/runs/{identifier}/pause",
            "POST",
            RunView,
            "run.pause",
            ManualControlRequest,
            idempotency=True,
        ),
        Operation(
            "runs.resume",
            ENVIRONMENT + "/runs/{identifier}/resume",
            "POST",
            RunView,
            "run.resume",
            ManualControlRequest,
            idempotency=True,
        ),
        Operation(
            "email_conversations.list",
            ENVIRONMENT + "/email/conversations",
            "GET",
            Page[EmailConversation],
            "email.read",
            page=True,
        ),
        Operation(
            "email_conversations.read",
            ENVIRONMENT + "/email/conversations/{identifier}",
            "GET",
            EmailConversationDetail,
            "email.read",
        ),
        Operation(
            "email_submissions.send",
            ENVIRONMENT + "/email/submissions",
            "POST",
            EmailSubmission,
            "email.send",
            EmailSendRequest,
            (202,),
        ),
        Operation(
            "email_submissions.reply",
            ENVIRONMENT + "/email/conversations/{identifier}/reply",
            "POST",
            EmailSubmission,
            "email.send",
            EmailReplyRequest,
            (202,),
        ),
        Operation(
            "email_submissions.read",
            ENVIRONMENT + "/email/submissions/{identifier}",
            "GET",
            EmailSubmission,
            "email.read",
        ),
        Operation(
            "email_submissions.execute",
            ENVIRONMENT + "/email/submissions/{identifier}/execute",
            "POST",
            EmailSubmission,
            "email.send",
        ),
        Operation(
            "email_receipts.list", ENVIRONMENT + "/email/receipts", "GET", Page[EmailReceipt], "email.read", page=True
        ),
        Operation(
            "email_sources.create",
            ENVIRONMENT + "/email/sources",
            "POST",
            EmailSourceResult,
            "email.manage",
            EmailSourceRequest,
            (201,),
        ),
        Operation(
            "email_sources.poll",
            ENVIRONMENT + "/email/sources/{identifier}/poll",
            "POST",
            EmailSourceStatus,
            "email.manage",
        ),
        Operation(
            "email_sources.rebaseline",
            ENVIRONMENT + "/email/sources/{identifier}/rebaseline",
            "POST",
            EmailSourceStatus,
            "email.manage",
        ),
        Operation(
            "email_receipts.correlate",
            ENVIRONMENT + "/email/receipts/{identifier}/correlate",
            "POST",
            EmailSourceStatus,
            "email.manage",
            EmailCorrelationRequest,
        ),
        Operation(
            "email_receipts.dispatch",
            ENVIRONMENT + "/email/receipts/{identifier}/dispatch",
            "POST",
            EmailSourceStatus,
            "email.manage",
        ),
        Operation(
            "email_tokens.create",
            ENVIRONMENT + "/email/correlation-tokens",
            "POST",
            EmailCorrelationToken,
            "email.manage",
            EmailTokenRequest,
            (201,),
        ),
        Operation(
            "email_tokens.revoke",
            ENVIRONMENT + "/email/correlation-tokens/{identifier}/revoke",
            "POST",
            EmailSourceStatus,
            "email.manage",
        ),
        Operation(
            "principals.list", "/api/v1/admin/principals", "GET", Page[PrincipalRecord], "grant.admin", page=True
        ),
        Operation(
            "principals.create",
            "/api/v1/admin/principals",
            "POST",
            PrincipalRecord,
            "grant.admin",
            PrincipalCreateRequest,
            (201,),
        ),
        Operation(
            "principals.link",
            "/api/v1/admin/principals/{identifier}/identity-links",
            "POST",
            PrincipalIdentityResult,
            "grant.admin",
            PrincipalIdentityRequest,
        ),
        Operation(
            "principals.status",
            "/api/v1/admin/principals/{identifier}/status",
            "POST",
            PrincipalRecord,
            "grant.admin",
            PrincipalStatusRequest,
        ),
        Operation(
            "members.list",
            "/tenants/{tenant}/members",
            "GET",
            Page[MemberBinding],
            "grant.manage or grant.admin",
            page=True,
        ),
        Operation(
            "members.grant",
            "/tenants/{tenant}/members",
            "POST",
            MemberBinding,
            "grant.manage or grant.admin",
            MemberGrantRequest,
            (201,),
        ),
        Operation(
            "members.revoke",
            "/tenants/{tenant}/members/{identifier}/revoke",
            "POST",
            Revoked,
            "grant.manage or grant.admin",
        ),
        Operation("identity.read", "/api/v1/identity", "GET", IdentityView, "authenticated identity"),
        Operation(
            "client_configuration.read",
            "/api/v1/client-configuration",
            "GET",
            ClientConfiguration,
            "",
            public=True,
            description="Public sign-in settings for CLI and Studio onboarding.",
        ),
        Operation("health.live", "/health/live", "GET", Health, "", public=True, description="Public health probe."),
        Operation("health.ready", "/health/ready", "GET", Health, "", public=True, description="Public health probe."),
        Operation("admin.tenant", "/admin/tenants", "POST", Identifier, "tenant.create", NameRequest),
        Operation("admin.grant", "/admin/grants", "POST", Identifier, "grant.admin or grant.manage", GrantRequest),
        Operation("projects.create", "/tenants/{tenant}/projects", "POST", Identifier, "project.manage", NameRequest),
        Operation(
            "environments.create", PROJECT + "/environments", "POST", Identifier, "environment.manage", NameRequest
        ),
        Operation("environments.read", ENVIRONMENT, "GET", NamedResource, "status.read"),
        Operation(
            "compiler.compile", PROJECT + "/compiler/compile", "POST", CompileResponse, "compile", CompilerRequest
        ),
        Operation(
            "compiler.validate", PROJECT + "/compiler/validate", "POST", CompileResponse, "compile", CompilerRequest
        ),
        Operation(
            "compiler.evaluate_decision",
            PROJECT + "/compiler/evaluate-decision",
            "POST",
            DecisionEvaluation,
            "compile",
            DecisionEvaluationRequest,
        ),
        Operation("catalog.read", PROJECT + "/catalog", "GET", CatalogLock, "catalog.read"),
        Operation("capabilities.read", PROJECT + "/capabilities", "GET", Capabilities, "catalog.read"),
        Operation("language.read", PROJECT + "/language", "GET", LanguageManifest, "catalog.read"),
        Operation("schemas.read", PROJECT + "/schemas", "GET", dict[str, JsonObjectData], "catalog.read"),
        # Static catalog resources precede the {collection} matcher for first-match consumers.
        Operation(
            "connector_descriptors.list",
            PROJECT + "/connector-descriptors",
            "GET",
            Page[ConnectorDescriptorView],
            "catalog.read",
            page=True,
        ),
        Operation(
            "connector_descriptors.read",
            PROJECT + "/connector-descriptors/{adapter}",
            "GET",
            ConnectorDescriptorView,
            "catalog.read",
        ),
        Operation(
            "definitions.publish",
            PROJECT + "/{collection}",
            "POST",
            PublishedVersion,
            "definition.publish",
            PublicationRequest,
            (201,),
            idempotency=True,
        ),
        Operation(
            "definitions.list",
            PROJECT + "/{collection}",
            "GET",
            Page[PublishedVersion | Draft | UnavailableResource | UnsupportedResource],
            "catalog.read",
            page=True,
        ),
        Operation(
            "definitions.read",
            PROJECT + "/{collection}/{identifier}",
            "GET",
            VersionView | DraftView,
            "catalog.read",
            etag=True,
        ),
        Operation(
            "definitions.export",
            PROJECT + "/{collection}/{identifier}/export",
            "GET",
            VersionExport | DraftExport,
            "catalog.read",
        ),
        Operation(
            "definitions.retire",
            PROJECT + "/{collection}/{identifier}/retire",
            "POST",
            RetiredVersion,
            "release.retire",
            RetirementRequest,
            idempotency=True,
            request_required=False,
        ),
        Operation(
            "drafts.save",
            PROJECT + "/drafts/{identifier}",
            "PUT",
            Draft,
            "definition.write",
            DraftRequest,
            (200, 201),
            revision="optional",
            etag=True,
        ),
        Operation(
            "drafts.retire",
            PROJECT + "/drafts/{identifier}",
            "DELETE",
            DraftRetirement,
            "definition.write",
            revision="required",
            etag=True,
        ),
        Operation(
            "activations.create",
            ENVIRONMENT + "/activations",
            "POST",
            Activation,
            "release.activate",
            ActivationRequest,
            (201,),
            revision="optional",
            idempotency=True,
            etag=True,
        ),
        Operation("activations.list", ENVIRONMENT + "/activations", "GET", Page[Activation], "catalog.read", page=True),
        Operation(
            "activations.read", ENVIRONMENT + "/activations/{identifier}", "GET", Activation, "catalog.read", etag=True
        ),
        Operation(
            "activations.export",
            ENVIRONMENT + "/activations/{identifier}/export",
            "GET",
            ActivationExport,
            "catalog.read",
        ),
        Operation(
            "activations.retire",
            ENVIRONMENT + "/activations/{identifier}/retire",
            "POST",
            Activation,
            "release.retire",
            RetirementRequest,
            revision="required",
            idempotency=True,
            etag=True,
            request_required=False,
        ),
        Operation(
            "connections.create",
            ENVIRONMENT + "/connections",
            "POST",
            ConnectionRevision,
            "connection.manage",
            ConnectionRequest,
            (201,),
            optional_idempotency=True,
        ),
        Operation(
            "bindable_connections.list",
            ENVIRONMENT + "/bindable-connections",
            "GET",
            Page[BindableConnection],
            "connection.bind",
            query=BindableConnectionQuery,
        ),
        Operation(
            "connections.list",
            ENVIRONMENT + "/connections",
            "GET",
            Page[ConnectionRevision | UnavailableResource],
            "connection.manage",
            page=True,
        ),
        Operation(
            "connections.read",
            ENVIRONMENT + "/connections/{identifier}",
            "GET",
            ConnectionRevision,
            "connection.manage",
        ),
        Operation(
            "connections.test",
            ENVIRONMENT + "/connections/{identifier}/test",
            "POST",
            ConnectionTestResult,
            "connection.manage",
            RetirementRequest,
            request_required=False,
        ),
        Operation(
            "ai_setup.publish",
            ENVIRONMENT + "/ai/setup/publish",
            "POST",
            AISetupPublishResult,
            "definition.publish",
            RetirementRequest,
            request_required=False,
        ),
        Operation(
            "ai_setup.grant",
            ENVIRONMENT + "/ai/setup/grant",
            "POST",
            AISetupGrantResult,
            "connection.manage",
            AISetupGrantRequest,
        ),
        Operation("ai_readiness.read", ENVIRONMENT + "/ai/readiness", "GET", AIReadinessResult, "catalog.read"),
        Operation("ai_endpoints.list", ENVIRONMENT + "/ai/endpoints", "GET", AIEndpointsResult, "catalog.read"),
        Operation(
            "ai_models.list", ENVIRONMENT + "/ai/models", "GET", AIModelsResult, "catalog.read", query=AIModelsQuery
        ),
        Operation(
            "ai_connections.test",
            ENVIRONMENT + "/ai/connections/{identifier}/test",
            "POST",
            AIConnectionTestResult,
            "connection.manage",
            AIConnectionTestRequest,
        ),
        Operation(
            "runs.start", ENVIRONMENT + "/runs", "POST", RunView, "run.start", StartRunRequest, (201,), idempotency=True
        ),
        Operation(
            "runs.list", ENVIRONMENT + "/runs", "GET", Page[RunView | UnavailableResource], "run.read", page=True
        ),
        Operation("runs.read", ENVIRONMENT + "/runs/{identifier}", "GET", RunView, "run.read"),
        Operation(
            "runs.signal",
            ENVIRONMENT + "/runs/{identifier}/signals",
            "POST",
            SignalReceipt,
            "run.signal",
            SignalRequest,
            (202,),
        ),
        Operation(
            "runs.cancel",
            ENVIRONMENT + "/runs/{identifier}/cancel",
            "POST",
            RunView | UnavailableRunAcknowledgment | CapacityRunAcknowledgment,
            "run.cancel",
            CancelRunRequest,
        ),
        Operation(
            "runs.retry",
            ENVIRONMENT + "/runs/{identifier}/retry",
            "POST",
            RunView,
            "run.retry",
            StartRunRequest,
            (201,),
            idempotency=True,
        ),
        Operation("runs.history", ENVIRONMENT + "/runs/{identifier}/history", "GET", EventPage, "run.read"),
        Operation("runs.export", ENVIRONMENT + "/runs/{identifier}/export", "GET", HistoryExport, "run.read"),
        Operation("runs.replay", ENVIRONMENT + "/runs/{identifier}/replay", "GET", ReplayReport, "run.read"),
        Operation(
            "run_summaries.list",
            ENVIRONMENT + "/run-summaries",
            "GET",
            RunSummaryPage,
            "run.read",
            query=RunSummaryQuery,
            served=False,
        ),
        Operation(
            "runs.steps",
            ENVIRONMENT + "/runs/{identifier}/steps",
            "GET",
            StepFactPage,
            "run.read",
            query=RunStepQuery,
            served=False,
        ),
        Operation(
            "runs.logs",
            ENVIRONMENT + "/runs/{identifier}/logs",
            "GET",
            RunLogPage,
            "run.read",
            query=RunLogQuery,
            served=False,
        ),
        Operation(
            "incidents.run_list",
            ENVIRONMENT + "/runs/{identifier}/incidents",
            "GET",
            Page[IncidentView | UnavailableResource],
            "incident.read",
            page=True,
        ),
        Operation(
            "incidents.list",
            ENVIRONMENT + "/incidents",
            "GET",
            Page[IncidentView | UnavailableResource],
            "incident.read",
            page=True,
        ),
        Operation(
            "incidents.resolve",
            ENVIRONMENT + "/incidents/{identifier}/resolve",
            "POST",
            IncidentView,
            "incident.resolve",
            IncidentResolution,
            revision="required",
            etag=True,
        ),
        Operation(
            "releases.create",
            ENVIRONMENT + "/worker-releases",
            "POST",
            WorkerRelease,
            "release.activate",
            ReleaseRequest,
            (201,),
        ),
        Operation(
            "releases.list", ENVIRONMENT + "/worker-releases", "GET", Page[WorkerRelease], "catalog.read", page=True
        ),
        Operation("releases.read", ENVIRONMENT + "/worker-releases/{identifier}", "GET", WorkerRelease, "catalog.read"),
        Operation(
            "workers.create",
            ENVIRONMENT + "/workers",
            "POST",
            WorkerInstance,
            "worker.register",
            InstanceRequest,
            (201,),
        ),
        Operation("workers.list", ENVIRONMENT + "/workers", "GET", Page[WorkerStatus], "status.read", page=True),
        Operation("workers.read", ENVIRONMENT + "/workers/{identifier}", "GET", WorkerStatus, "status.read"),
        *(
            Operation(
                "workers." + action,
                ENVIRONMENT + "/workers/{identifier}/" + action,
                "POST",
                WorkerStatus,
                "worker.drain",
                WorkerControlRequest,
                idempotency=True,
            )
            for action in ("drain", "resume")
        ),
        Operation("workers.revoke", ENVIRONMENT + "/workers/{identifier}/revoke", "POST", Revoked, "release.retire"),
        Operation(
            "workers.grant",
            ENVIRONMENT + "/worker-connection-grants",
            "POST",
            Granted,
            "connection.manage",
            CredentialGrantRequest,
            (201,),
        ),
        Operation("tasks.claim", ENVIRONMENT + "/tasks/claim", "POST", list[TaskLease], "task.claim", ClaimRequest),
        Operation(
            "tasks.context", ENVIRONMENT + "/tasks/context", "POST", TaskExecutionContext, "task.claim", LeaseProof
        ),
        Operation("tasks.heartbeat", ENVIRONMENT + "/tasks/heartbeat", "POST", TaskLease, "task.heartbeat", LeaseProof),
        Operation(
            "tasks.complete",
            ENVIRONMENT + "/tasks/complete",
            "POST",
            CompletionAcknowledgment,
            "task.complete",
            CompleteRequest,
        ),
        Operation(
            "tasks.fail", ENVIRONMENT + "/tasks/fail", "POST", CompletionAcknowledgment, "task.complete", FailRequest
        ),
        Operation(
            "tasks.credentials",
            ENVIRONMENT + "/tasks/credentials",
            "POST",
            CredentialResponse,
            "credential.lease",
            CredentialRequest,
        ),
        Operation(
            "subscriptions.save",
            ENVIRONMENT + "/subscriptions",
            "POST",
            Subscription,
            "subscription.manage and connection.manage and connection.bind and source authority",
            SubscriptionRequest,
            (201,),
        ),
        Operation(
            "subscriptions.list", ENVIRONMENT + "/subscriptions", "GET", Page[Subscription], "delivery.read", page=True
        ),
        Operation(
            "subscriptions.read", ENVIRONMENT + "/subscriptions/{identifier}", "GET", Subscription, "delivery.read"
        ),
        Operation(
            "subscriptions.disable",
            ENVIRONMENT + "/subscriptions/{identifier}/disable",
            "POST",
            Subscription,
            "subscription.manage and source authority",
        ),
        Operation(
            "deliveries.list", ENVIRONMENT + "/deliveries", "GET", Page[DeliveryView], "delivery.read", page=True
        ),
        Operation("deliveries.read", ENVIRONMENT + "/deliveries/{identifier}", "GET", DeliveryView, "delivery.read"),
        Operation(
            "deliveries.retry",
            ENVIRONMENT + "/deliveries/{identifier}/retry",
            "POST",
            DeliveryView,
            "delivery.retry and current source authority",
        ),
        Operation(
            "deliveries.history",
            ENVIRONMENT + "/deliveries/{identifier}/attempts",
            "GET",
            Page[DeliveryAttempt],
            "delivery.read",
            page=True,
        ),
        Operation(
            "source_bindings.list",
            ENVIRONMENT + "/connection-source-bindings",
            "GET",
            Page[SourceBinding],
            "connection.manage",
            page=True,
        ),
        Operation(
            "source_bindings.read",
            ENVIRONMENT + "/connection-source-bindings/{identifier}",
            "GET",
            SourceBinding,
            "connection.manage",
        ),
        Operation(
            "source_bindings.revoke",
            ENVIRONMENT + "/connection-source-bindings/{identifier}/revoke",
            "POST",
            Revoked,
            "connection.manage",
        ),
        Operation(
            "broker_triggers.create",
            ENVIRONMENT + "/broker-triggers",
            "POST",
            BrokerTrigger,
            "trigger.manage and connection.manage and connection.bind and target authority",
            BrokerTriggerRequest,
            (201,),
        ),
        Operation(
            "broker_triggers.list",
            ENVIRONMENT + "/broker-triggers",
            "GET",
            Page[BrokerTrigger],
            "trigger.manage",
            page=True,
        ),
        Operation(
            "broker_triggers.read",
            ENVIRONMENT + "/broker-triggers/{identifier}",
            "GET",
            BrokerTrigger,
            "trigger.manage",
        ),
        Operation(
            "broker_triggers.disable",
            ENVIRONMENT + "/broker-triggers/{identifier}/disable",
            "POST",
            BrokerTrigger,
            "trigger.manage",
        ),
        Operation(
            "broker_triggers.retry",
            ENVIRONMENT + "/broker-triggers/{identifier}/retry",
            "POST",
            BrokerTrigger,
            "trigger.manage and current source authority",
        ),
        Operation(
            "broker_triggers.incidents",
            ENVIRONMENT + "/broker-incidents",
            "GET",
            Page[BrokerIncident],
            "trigger.manage",
            page=True,
        ),
        Operation(
            "triggers.create", ENVIRONMENT + "/triggers", "POST", Trigger, "trigger.manage", TriggerRequest, (201,)
        ),
        Operation("triggers.list", ENVIRONMENT + "/triggers", "GET", Page[Trigger], "trigger.manage", page=True),
        Operation("triggers.read", ENVIRONMENT + "/triggers/{identifier}", "GET", Trigger, "trigger.manage"),
        Operation(
            "triggers.disable", ENVIRONMENT + "/triggers/{identifier}/disable", "POST", Disabled, "trigger.manage"
        ),
        Operation(
            "webhooks.receive",
            "/webhooks/{identifier}",
            "POST",
            TriggerReceipt,
            "signed webhook and pinned principal authority",
            WebhookEnvelope,
            (202,),
        ),
        Operation(
            "schedules.save",
            ENVIRONMENT + "/schedules",
            "POST",
            ScheduleView,
            "trigger.manage and run.start",
            ScheduleRequest,
            (201,),
            revision="optional",
            etag=True,
        ),
        Operation("schedules.list", ENVIRONMENT + "/schedules", "GET", Page[ScheduleView], "run.read", page=True),
        Operation("schedules.read", ENVIRONMENT + "/schedules/{identifier}", "GET", ScheduleView, "run.read"),
        Operation(
            "schedules.history",
            ENVIRONMENT + "/schedules/{identifier}/occurrences",
            "GET",
            Page[ScheduleOccurrence],
            "run.read",
            page=True,
        ),
        *(
            Operation(
                "schedules." + action,
                ENVIRONMENT + "/schedules/{identifier}/" + action,
                "POST",
                ScheduleView,
                "trigger.manage",
                revision="required",
                etag=True,
            )
            for action in ("enable", "disable", "delete")
        ),
        Operation(
            "debug.create",
            PROJECT + "/debug/sessions",
            "POST",
            DebugSession,
            "simulate",
            DebugCreate,
            (201,),
            etag=True,
        ),
        Operation("debug.read", PROJECT + "/debug/sessions/{identifier}", "GET", DebugSession, "simulate", etag=True),
        Operation(
            "debug.command",
            PROJECT + "/debug/sessions/{identifier}/commands",
            "POST",
            DebugSession,
            "simulate",
            DebugCommand,
            revision="required",
            etag=True,
        ),
    )
}
