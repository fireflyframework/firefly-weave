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

"""Fresh, normalized release schemas derived from strict contract models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any, cast

import rfc8785
from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from firefly_weave.contracts.definitions import (
    ActionDefinition,
    ConnectorDefinition,
    DecisionTableDefinition,
    Definition,
    WorkerImplementation,
    WorkerRouting,
    WorkflowDefinition,
)
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.values import JsonObject

DIALECT = "https://json-schema.org/draft/2020-12/schema"
type SchemaModel = type[BaseModel] | TypeAdapter[Any]


class _ContractSchemaGenerator(GenerateJsonSchema):
    def chain_schema(self, schema: core_schema.ChainSchema) -> JsonSchemaValue:
        # Pydantic's default first-step export drops constraints attached after BeforeValidator.
        return {"allOf": [self.generate_inner(step) for step in schema["steps"]]}


def _stable_definitions(schema: dict[str, Any]) -> None:
    definitions = schema.get("$defs", {})
    renames = {
        name: "Constraint_" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()[:16]
        for name, value in definitions.items()
        if name.startswith("UnicodeString_")
    }
    schema["$defs"] = {renames.get(name, name): value for name, value in definitions.items()}

    for node in _schema_nodes(schema):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in renames:
                node["$ref"] = "#/$defs/" + renames[name]


def contract_models() -> dict[str, SchemaModel]:
    from firefly_weave.compiler.catalog import CatalogLock
    from firefly_weave.compiler.ir import ArtifactEnvelope, Executable
    from firefly_weave.contracts.broker import (
        BrokerIncident,
        BrokerReceipt,
        BrokerTrigger,
        BrokerTriggerRequest,
        SourceBinding,
    )
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
    from firefly_weave.contracts.connector_descriptors import ConnectorDescriptorView
    from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionRevision, ConnectionTestResult
    from firefly_weave.contracts.deployments import (
        ApplyPlanRequest,
        ComponentSpec,
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
        ObservationReport,
        ObservedResource,
        ObserveRequest,
        PlanApproval,
        PlanApprovalRequest,
        PlanRequest,
        PlanStep,
        ReconcileJobRequest,
        RunnerClaimRequest,
        RunnerRegistration,
        RunnerReport,
        SafeDeploymentReceipt,
        TargetRequest,
        TargetUpdate,
    )
    from firefly_weave.contracts.email import (
        EmailConversation,
        EmailConversationDetail,
        EmailCorrelationRequest,
        EmailCorrelationToken,
        EmailMessage,
        EmailReceipt,
        EmailReplyRequest,
        EmailSendRequest,
        EmailSourceRequest,
        EmailSourceResult,
        EmailSourceStatus,
        EmailSubmission,
        EmailTokenRequest,
        MailProfile,
    )
    from firefly_weave.contracts.file_workers import WorkerFileAccess, WorkerFileChunk, WorkerFileCreate, WorkerFileRead
    from firefly_weave.contracts.files import FileChunk, FileChunkRead, FileCreate, FileReference, FileUpload
    from firefly_weave.contracts.http_profiles import AuthProfile, HttpOperation, ProfileConnection
    from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
    from firefly_weave.contracts.human_tasks import (
        AssignmentBinding,
        AssignmentBindingList,
        AssignmentBindingRequest,
        AssignmentPin,
        CompleteHumanTask,
        HumanTask,
        HumanTaskCommand,
        ManualControlRequest,
        ReassignHumanTask,
        TaskGroup,
        TaskGroupRequest,
    )
    from firefly_weave.contracts.identity import IdentityView
    from firefly_weave.contracts.language import LanguageManifest
    from firefly_weave.contracts.lumi import (
        LumiAskRequest,
        LumiConfiguration,
        LumiConfigurationRequest,
        LumiReply,
        LumiStatus,
    )
    from firefly_weave.contracts.members import (
        MemberBinding,
        MemberGrantRequest,
        PrincipalCreateRequest,
        PrincipalIdentityRequest,
        PrincipalIdentityResult,
        PrincipalRecord,
        PrincipalStatusRequest,
    )
    from firefly_weave.contracts.operations import EventPage, HistoryExport, RecordedEvidence, ReplayReport
    from firefly_weave.contracts.providers import (
        ProviderEvent,
        ProviderIngressResponse,
        ProviderReceipt,
        ProviderSource,
        ProviderSourceRequest,
    )
    from firefly_weave.contracts.public import (
        Capabilities,
        CompileResponse,
        CompilerRequest,
        DecisionEvaluation,
        DecisionEvaluationRequest,
        DraftExport,
        DraftRetirement,
        DraftView,
        Problem,
        VersionExport,
        VersionView,
    )
    from firefly_weave.contracts.run_lifecycle import RunLifecycle, RunLifecycleRequest, RunPurgeRequest
    from firefly_weave.contracts.run_views import (
        RunListQuery,
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
        StartRunRequest,
        UnavailableRunAcknowledgment,
    )
    from firefly_weave.contracts.schedules import ScheduleOccurrence, ScheduleRequest, ScheduleView
    from firefly_weave.contracts.teams import TeamsReactivateRequest, TeamsReference, TeamsRevokeRequest
    from firefly_weave.contracts.whatsapp import WhatsAppDeliveryState, WhatsAppStatusFact
    from firefly_weave.contracts.workers import (
        ClaimRequest,
        CompleteRequest,
        CompletionAcknowledgment,
        CompletionReceipt,
        CredentialGrantRequest,
        CredentialLease,
        CredentialRequest,
        FailRequest,
        InstanceRequest,
        LeaseProof,
        ReleaseRequest,
        TaskError,
        TaskLease,
        UnavailableCompletionReceipt,
        WorkerControlRequest,
        WorkerInstance,
        WorkerRelease,
        WorkerStatus,
    )
    from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugSession, DebugView
    from firefly_weave.operations.redaction import SafeProjection
    from firefly_weave.sdk.openapi_import import OpenAPIImportPolicy
    from firefly_weave.triggers.models import Trigger, TriggerReceipt, TriggerRequest

    return {
        "operations-target-request": TargetRequest,
        "operations-deployment-target": DeploymentTarget,
        "operations-target-update": TargetUpdate,
        "operations-component-spec": ComponentSpec,
        "operations-deployment-request": DeploymentRequest,
        "operations-deployment": Deployment,
        "operations-observed-resource": ObservedResource,
        "operations-observation-report": ObservationReport,
        "operations-deployment-observation": DeploymentObservation,
        "operations-plan-request": PlanRequest,
        "operations-plan-step": PlanStep,
        "operations-deployment-plan": DeploymentPlan,
        "operations-plan-approval-request": PlanApprovalRequest,
        "operations-plan-approval": PlanApproval,
        "operations-apply-plan-request": ApplyPlanRequest,
        "operations-observe-request": ObserveRequest,
        "operations-job-cancel-request": JobCancelRequest,
        "operations-safe-deployment-receipt": SafeDeploymentReceipt,
        "operations-deployment-job": DeploymentJob,
        "operations-runner-registration": RunnerRegistration,
        "operations-deployment-runner": DeploymentRunner,
        "operations-runner-claim-request": RunnerClaimRequest,
        "operations-deployment-lease-proof": DeploymentLeaseProof,
        "operations-deployment-lease": DeploymentLease,
        "operations-runner-report": RunnerReport,
        "operations-reconcile-job-request": ReconcileJobRequest,
        "human-file-create": HumanFileCreate,
        "human-file-command": HumanFileCommand,
        "human-file-chunk": HumanFileChunk,
        "human-file-read": HumanFileRead,
        "file-create": FileCreate,
        "file-reference": FileReference,
        "file-upload": FileUpload,
        "file-chunk": FileChunk,
        "file-chunk-read": FileChunkRead,
        "worker-file-create": WorkerFileCreate,
        "worker-file-access": WorkerFileAccess,
        "worker-file-chunk": WorkerFileChunk,
        "worker-file-read": WorkerFileRead,
        "teams-reference": TeamsReference,
        "teams-reactivate-request": TeamsReactivateRequest,
        "teams-revoke-request": TeamsRevokeRequest,
        "whatsapp-delivery-state": WhatsAppDeliveryState,
        "whatsapp-status-fact": WhatsAppStatusFact,
        "provider-source-request": ProviderSourceRequest,
        "provider-source": ProviderSource,
        "provider-event": ProviderEvent,
        "provider-receipt": ProviderReceipt,
        "identity-view": IdentityView,
        "client-configuration": ClientConfiguration,
        "member-binding": MemberBinding,
        "member-grant-request": MemberGrantRequest,
        "principal-create-request": PrincipalCreateRequest,
        "principal-identity-request": PrincipalIdentityRequest,
        "principal-identity-result": PrincipalIdentityResult,
        "principal-record": PrincipalRecord,
        "principal-status-request": PrincipalStatusRequest,
        "run-lifecycle": RunLifecycle,
        "run-lifecycle-request": RunLifecycleRequest,
        "run-purge-request": RunPurgeRequest,
        "run-summary-page": RunSummaryPage,
        "run-summary-query": RunSummaryQuery,
        "run-list-query": RunListQuery,
        "step-fact-page": StepFactPage,
        "run-step-query": RunStepQuery,
        "run-log-page": RunLogPage,
        "run-log-query": RunLogQuery,
        "human-task": HumanTask,
        "human-task-command": HumanTaskCommand,
        "complete-human-task": CompleteHumanTask,
        "reassign-human-task": ReassignHumanTask,
        "manual-control-request": ManualControlRequest,
        "assignment-binding": AssignmentBinding,
        "assignment-binding-list": AssignmentBindingList,
        "assignment-binding-request": AssignmentBindingRequest,
        "assignment-pin": AssignmentPin,
        "task-group": TaskGroup,
        "task-group-request": TaskGroupRequest,
        "email-send-request": EmailSendRequest,
        "email-reply-request": EmailReplyRequest,
        "email-submission": EmailSubmission,
        "email-conversation": EmailConversation,
        "email-conversation-detail": EmailConversationDetail,
        "email-message": EmailMessage,
        "email-source-request": EmailSourceRequest,
        "email-source-status": EmailSourceStatus,
        "email-source-result": EmailSourceResult,
        "email-receipt": EmailReceipt,
        "email-correlation-request": EmailCorrelationRequest,
        "email-token-request": EmailTokenRequest,
        "email-correlation-token": EmailCorrelationToken,
        "mail-profile": MailProfile,
        "provider-ingress-response": ProviderIngressResponse,
        "broker-trigger": BrokerTrigger,
        "broker-trigger-request": BrokerTriggerRequest,
        "broker-receipt": TypeAdapter(BrokerReceipt),
        "broker-incident": BrokerIncident,
        "source-binding": SourceBinding,
        "compiler-request": CompilerRequest,
        "compile-response": CompileResponse,
        "decision-evaluation-request": DecisionEvaluationRequest,
        "decision-evaluation": DecisionEvaluation,
        "problem": Problem,
        "capabilities": Capabilities,
        "language-manifest": LanguageManifest,
        "draft-view": DraftView,
        "draft-retirement": DraftRetirement,
        "draft-export": DraftExport,
        "version-view": VersionView,
        "version-export": VersionExport,
        "schedule-request": ScheduleRequest,
        "schedule-view": ScheduleView,
        "schedule-occurrence": ScheduleOccurrence,
        "event-page": EventPage,
        "history-export": HistoryExport,
        "recorded-evidence": RecordedEvidence,
        "replay-report": ReplayReport,
        "trigger": Trigger,
        "trigger-request": TriggerRequest,
        "trigger-receipt": TriggerReceipt,
        "activation": Activation,
        "activation-request": ActivationRequest,
        "draft": Draft,
        "draft-request": DraftRequest,
        "publication-request": PublicationRequest,
        "published-version": PublishedVersion,
        "retirement-request": RetirementRequest,
        "connection-request": ConnectionRequest,
        "connection-revision": ConnectionRevision,
        "connection-test-result": ConnectionTestResult,
        "connector-descriptor": ConnectorDescriptorView,
        # HTTP profile 2.0.0 authoring contracts; the profile models remain the final authority.
        "http-operation": HttpOperation,
        "http-profile-connection": ProfileConnection,
        "auth-profile": AuthProfile,
        "openapi-import-policy": OpenAPIImportPolicy,
        "run-view": RunView,
        "safe-projection": SafeProjection,
        "debug-create": DebugCreate,
        "debug-command": DebugCommand,
        "debug-view": DebugView,
        "debug-session": DebugSession,
        "unavailable-run-acknowledgment": UnavailableRunAcknowledgment,
        "capacity-run-acknowledgment": CapacityRunAcknowledgment,
        "start-run-request": StartRunRequest,
        "worker-release-request": ReleaseRequest,
        "worker-release": WorkerRelease,
        "worker-instance-request": InstanceRequest,
        "worker-instance": WorkerInstance,
        "worker-status": WorkerStatus,
        "worker-control-request": WorkerControlRequest,
        "lease-proof": LeaseProof,
        "task-lease": TaskLease,
        "completion-receipt": CompletionReceipt,
        "unavailable-completion-receipt": UnavailableCompletionReceipt,
        "completion-acknowledgment": TypeAdapter(CompletionAcknowledgment),
        "task-error": TaskError,
        "claim-request": ClaimRequest,
        "complete-request": CompleteRequest,
        "fail-request": FailRequest,
        "credential-request": CredentialRequest,
        "credential-grant-request": CredentialGrantRequest,
        "credential-lease": CredentialLease,
        "catalog-lock": CatalogLock,
        "executable": TypeAdapter(Executable),
        "compiled-artifact": ArtifactEnvelope,
        "definition": TypeAdapter(Definition),
        "lumi-ask-request": LumiAskRequest,
        "lumi-reply": LumiReply,
        "lumi-configuration-request": LumiConfigurationRequest,
        "lumi-configuration": LumiConfiguration,
        "lumi-status": LumiStatus,
        "workflow": WorkflowDefinition,
        "action": ActionDefinition,
        "connector": ConnectorDefinition,
        "decisiontable": DecisionTableDefinition,
        "diagnostic": Diagnostic,
        "worker-implementation": WorkerImplementation,
        "worker-routing": WorkerRouting,
    }


def _schema_nodes(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield schema
    for keyword in ("$defs", "properties", "patternProperties", "dependentSchemas"):
        for child in schema.get(keyword, {}).values():
            if isinstance(child, dict):
                yield from _schema_nodes(child)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(keyword, []):
            if isinstance(child, dict):
                yield from _schema_nodes(child)
    for keyword in ("items", "additionalProperties", "contains", "propertyNames", "not", "if", "then", "else"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            yield from _schema_nodes(child)


def _normalize(schema: dict[str, Any]) -> None:
    for node in _schema_nodes(schema):
        node.pop("discriminator", None)


def export_schemas(*, extra_models: Mapping[str, SchemaModel] | None = None) -> dict[str, JsonObject]:
    """Export actual public contracts; core contract names cannot be replaced."""
    models = contract_models()
    for name, model in (extra_models or {}).items():
        if name in models or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None:
            raise ValueError("Additional contract names must be unique lowercase identifiers")
        models[name] = model
    result: dict[str, JsonObject] = {}
    for name, model in sorted(models.items()):
        schema = (
            model.json_schema(by_alias=True, schema_generator=_ContractSchemaGenerator)
            if isinstance(model, TypeAdapter)
            else model.model_json_schema(by_alias=True, schema_generator=_ContractSchemaGenerator)
        )
        _normalize(schema)
        _stable_definitions(schema)
        schema["$schema"] = DIALECT
        schema["$id"] = f"{name}.schema.json"
        # Canonical roundtrip strips framework containers and stabilizes artifact ordering.
        result[name] = cast(JsonObject, json.loads(rfc8785.dumps(schema)))
    return result


def schema_snapshot(*, extra_models: Mapping[str, SchemaModel] | None = None) -> bytes:
    """Canonical release bytes; callers choose the artifact destination outside the source tree."""
    return rfc8785.dumps(export_schemas(extra_models=extra_models))
