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

"""Authorization precedes project binding; all mutations commit with audit and replay."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial as bind_call
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pyfly.container import Provider, service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import CompileResult, compile_source, import_artifact, validate_source
from firefly_weave.compiler.catalog import CatalogLock, CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.decision_tables import DecisionFailure, evaluate_decision_table
from firefly_weave.compiler.expressions import ExpressionFailure, measure_value
from firefly_weave.compiler.ir import DecisionTableIR, UnsupportedIR
from firefly_weave.compiler.parser import parse_source
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest, DefinitionKind, Draft, PublishedVersion
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.public import DecisionEvaluation, DecisionEvaluationRequest, DraftRetirement, DraftView
from firefly_weave.contracts.values import JsonObject
from firefly_weave.contracts.workers import ConnectorExecutionPin
from firefly_weave.definitions.models import CatalogError, ir_unsupported
from firefly_weave.definitions.ports import ConnectionBindingPort, WorkerAdmissionPort
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.operations.execution import execute_pure
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork


def project_scope(scope: Scope) -> Scope:
    if scope.project_id is None:
        raise CatalogError(422, "WV-SCOPE", "A project is required")
    return Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)


def published(row: dict[str, Any]) -> PublishedVersion:
    return PublishedVersion.model_validate({key: row[key] for key in PublishedVersion.model_fields})


def check_revision(actual: int | None, expected: int | None) -> None:
    if actual != expected:
        raise CatalogError(412, "WV-ETAG", "Revision precondition failed")


@service
class DefinitionService:
    def __init__(
        self,
        uow: UnitOfWork,
        authorization: AuthorizationService,
        registry: ConnectorRegistry,
        connections: Provider[ConnectionBindingPort],
        workers: Provider[WorkerAdmissionPort],
        outbox: OutboxService,
    ) -> None:
        self.uow = uow
        self.authorization = authorization
        self.registry = registry
        self.connections = connections
        self.workers = workers
        self.outbox = outbox

    @property
    def capabilities(self) -> CatalogSnapshot:
        return self.registry.snapshot()

    async def runtime_snapshot(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext, tx: Transaction
    ) -> tuple[Activation, dict[str, Any]]:
        """Resolve an immutable execution pin under environment run authority."""
        self.require(actor, scope, "run.start", context)
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Run requires an environment")
        async with self.transaction(scope, tx, mutation=False) as enlisted:
            repository = DefinitionRepository(enlisted)
            rows = await repository.rows(
                "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND project_id=:project "
                "AND environment_id=:environment AND id=:id",
                environment=scope.environment_id,
                id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Activation not found")
            activation = Activation.model_validate_json(json.dumps(rows[0]["payload"]))
            latest = await repository.rows(
                "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND project_id=:project "
                "AND environment_id=:environment AND name=:name ORDER BY revision DESC LIMIT 1",
                environment=scope.environment_id,
                name=activation.name,
            )
            row = await repository.version(activation.request.version_id)
            if activation.retired or latest[0]["payload"]["retired"] or row["retired"]:
                raise CatalogError(422, "WV-READINESS", "Retired activation or workflow cannot start")
            if row["digest"] != activation.request.artifact_digest:
                raise CatalogError(422, "WV-READINESS", "Activation artifact mismatch")
            return activation, row["artifact"]

    async def compiler_capabilities(self, tx: Transaction) -> CatalogSnapshot:
        return await self.workers.get().capabilities(tx, self.capabilities)

    async def execution_readiness(
        self,
        actor: Principal,
        scope: Scope,
        request: ActivationRequest,
        artifact: Any,
        *,
        capability: str,
        context: AuditContext,
        tx: Transaction,
    ) -> list[ConnectorExecutionPin]:
        return await self.workers.get().admit(
            actor, scope, request, artifact, capability=capability, context=context, tx=tx
        )

    async def connector_contract(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        capability: str,
        context: AuditContext,
        tx: Transaction,
    ) -> dict[str, Any]:
        if capability not in {"connection.manage", "connection.bind"}:
            raise AccessDenied()
        self.require(actor, scope, capability, context)
        async with self.transaction(scope, tx, mutation=False) as enlisted:
            row = await DefinitionRepository(enlisted).version(identifier)
            if row["kind"] != "Connector" or row["retired"]:
                raise CatalogError(422, "WV-CONNECTION", "Connector contract unavailable")
            return row

    def require(self, actor: Principal, scope: Scope, capability: str, context: AuditContext) -> None:
        self.authorization.require(actor, scope, capability, context=context)

    @asynccontextmanager
    async def transaction(
        self, scope: Scope, supplied: Transaction | None, *, mutation: bool = True
    ) -> AsyncIterator[Transaction]:
        if supplied is None:
            async with self.uow.open(scope, mutation=mutation) as owned:
                yield owned
            return
        current = supplied.session.get_transaction()
        if current is None or not current.is_active:
            raise CatalogError(422, "WV-TRANSACTION", "An active caller-owned transaction is required")
        if (
            supplied.scope.tenant_id != scope.tenant_id
            or supplied.scope.project_id != scope.project_id
            or (supplied.scope.environment_id is not None and supplied.scope.environment_id != scope.environment_id)
        ):
            raise AccessDenied()
        bound_tenant = await supplied.session.scalar(text("SELECT current_setting('weave.tenant_id',true)"))
        if bound_tenant != str(scope.tenant_id):
            raise AccessDenied()
        if (
            mutation
            and scope.project_id is not None
            and supplied.session.info.get("weave_admission") != (current, scope.tenant_id, scope.project_id)
        ):
            raise CatalogError(422, "WV-TRANSACTION", "Use an admitted outer UnitOfWork transaction")
        # Enlist only: the caller retains commit, rollback and session lifecycle ownership.
        yield Transaction(session=supplied.session, scope=scope, telemetry=supplied.telemetry)

    async def compile(
        self,
        actor: Principal,
        scope: Scope,
        source: str | JsonObject,
        format: Literal["yaml", "json", "object"],
        *,
        catalog: CatalogLock | None = None,
        filename: str | None = None,
        strict: bool = False,
        partial: bool = False,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> CompileResult:
        from firefly_weave.access.repository import load_principal

        scope = project_scope(scope)
        self.require(actor, scope, "compile", context)
        async with self.transaction(scope, tx, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "compile", context)
            if partial and catalog is None:
                return await execute_pure(bind_call(validate_source, source, format=format, filename=filename))
            snapshot = (
                CatalogSnapshot.from_lock(catalog.model_dump(by_alias=True))
                if catalog is not None
                else await DefinitionRepository(tx).snapshot(await self.compiler_capabilities(tx))
            )
            return await execute_pure(
                bind_call(
                    compile_source,
                    source,
                    format=format,
                    catalog=snapshot,
                    filename=filename,
                    strict=strict,
                    action_validators=self.registry.action_validators(),
                )
            )

    async def evaluate_decision(
        self, actor: Principal, scope: Scope, request: DecisionEvaluationRequest, *, context: AuditContext
    ) -> DecisionEvaluation:
        result = await self.compile(
            actor,
            scope,
            request.source,
            request.format,
            catalog=request.catalog,
            filename=request.filename,
            strict=request.strict,
            context=context,
        )
        if not result.ok or result.artifact is None:
            raise CatalogError(422, "WV-COMPILE", "Decision compilation failed", result=json.loads(result.to_bytes()))
        executable = result.artifact.executable
        if executable["kind"] != "DecisionTable":
            raise CatalogError(422, "WV-DECISION-CONTRACT", "A DecisionTable definition is required")
        table = DecisionTableIR.model_validate(executable)
        try:
            decision = await execute_pure(
                bind_call(
                    evaluate_decision_table,
                    table.spec.model_dump(by_alias=True),
                    request.input,
                    bundle={d.reference: d.document for d in table.dependencies if d.kind == "Schema"},
                )
            )
        except (DecisionFailure, ExpressionFailure) as error:
            raise CatalogError(422, error.code, "Decision evaluation failed", result={"path": error.path}) from None
        return DecisionEvaluation(
            output=decision.output, matched_rule_ids=list(decision.matched_rule_ids), used_default=decision.used_default
        )

    async def publish(
        self,
        actor: Principal,
        scope: Scope,
        kind: DefinitionKind,
        source: str,
        format: Literal["yaml", "json"],
        idempotency_key: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> PublishedVersion:
        scope = project_scope(scope)
        self.require(actor, scope, "definition.publish", context)
        async with self.transaction(scope, tx) as tx:
            replay = Idempotency(
                tx, actor.id, "publish:" + kind, idempotency_key, {"kind": kind, "source": source, "format": format}
            )
            prior = await replay.replay()
            if prior is not None:
                pinned = await DefinitionRepository(tx).version(UUID(prior["id"]))
                try:
                    import_artifact(pinned["artifact"])
                except ValueError:
                    raise CatalogError(
                        409, "WV-LEGACY-UNAVAILABLE", "Legacy publication evidence is unavailable"
                    ) from None
                return PublishedVersion.model_validate_json(json.dumps(prior))
            repository = DefinitionRepository(tx)
            snapshot = await repository.snapshot(await self.compiler_capabilities(tx))
            result = await execute_pure(
                bind_call(
                    compile_source,
                    source,
                    format=format,
                    catalog=snapshot,
                    action_validators=self.registry.action_validators(),
                )
            )
            from firefly_weave.access.repository import load_principal

            self.require(await load_principal(tx.session, actor.id), scope, "definition.publish", context)
            if any(d.code == "WV-COMP-IMMUTABLE_VERSION" for d in result.diagnostics):
                raise CatalogError(
                    409,
                    "WV-VERSION-CONFLICT",
                    "An immutable version already has different content",
                    result=json.loads(result.to_bytes()),
                )
            if not result.ok or result.artifact is None:
                raise CatalogError(
                    422, "WV-COMPILE", "Definition compilation failed", result=json.loads(result.to_bytes())
                )
            model = load_definition(parse_source(source, format=format).value)
            if model.kind != kind:
                raise CatalogError(422, "WV-KIND", "Definition kind does not match collection")
            document = model.model_dump(by_alias=True)
            if model.kind == "Connector":
                self.registry.validate_manifest(document)
            definition_digest = FrozenDocument.from_value(document).digest
            artifact = result.artifact
            await lock(
                tx,
                f"version:{scope.tenant_id}:{scope.project_id}:{kind}:{model.metadata.name}:{model.metadata.version}",
            )
            rows = await repository.rows(
                "SELECT * FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project "
                "AND kind=:kind AND name=:name AND version=:version",
                kind=kind,
                name=model.metadata.name,
                version=model.metadata.version,
            )
            if rows:
                version = published(rows[0])
                if version.digest != artifact.digest or version.definition_digest != definition_digest:
                    raise CatalogError(409, "WV-VERSION-CONFLICT", "An immutable version already has different content")
            else:
                version = PublishedVersion(
                    id=uuid4(),
                    kind=kind,
                    name=model.metadata.name,
                    version=model.metadata.version,
                    digest=artifact.digest,
                    definition_digest=definition_digest,
                )
                await repository.execute(
                    "INSERT INTO definition_versions "
                    "VALUES(:id,:tenant,:project,:kind,:name,:version,:digest,:definition_digest,cast(:document "
                    "AS jsonb),cast(:artifact AS jsonb))",
                    **version.model_dump(),
                    document=json.dumps(document),
                    artifact=artifact.to_bytes().decode(),
                )
            await repository.execute(
                "INSERT INTO "
                "definition_sources(id,tenant_id,project_id,version_id,source,format,source_hash,envelope) "
                "VALUES(:id,:tenant,:project,:version,:source,:format,:hash,cast(:envelope AS jsonb)) "
                "ON CONFLICT DO NOTHING",
                id=uuid4(),
                version=version.id,
                source=source,
                format=format,
                hash=artifact.source_hash,
                envelope=artifact.to_bytes().decode(),
            )
            await audit(
                tx.session,
                actor,
                "definition.publish",
                str(version.id),
                scope=scope,
                capability="definition.publish",
                context=context,
                details={"digest": version.digest, "kind": kind},
            )
            if not rows:
                await self.outbox.append(
                    tx,
                    IntegrationEvent(
                        event_id=uuid5(NAMESPACE_URL, "publication:" + str(version.id)),
                        type="definition.published",
                        scope=scope,
                        resource_id=version.id,
                        correlation_id=version.id,
                        emitted_at=await tx.session.scalar(text("SELECT clock_timestamp()")),
                        payload=EventMetadata(),
                    ),
                )
            await replay.save(version.model_dump(mode="json"))
            return version

    async def save_draft(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        document: JsonObject,
        expected_revision: int | None,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> Draft:
        scope = project_scope(scope)
        self.require(actor, scope, "definition.write", context)
        measure_value(document)
        async with self.transaction(scope, tx) as tx:
            await lock(tx, f"draft:{scope.tenant_id}:{scope.project_id}:{identifier}")
            repository = DefinitionRepository(tx)
            rows = await repository.rows(
                "SELECT revision FROM draft_revisions WHERE tenant_id=:tenant AND project_id=:project "
                "AND id=:id ORDER BY revision DESC LIMIT 1",
                id=identifier,
            )
            self.require(await load_principal(tx.session, actor.id), scope, "definition.write", context)
            if await repository.rows(
                "SELECT id FROM draft_retirements WHERE tenant_id=:tenant AND project_id=:project AND id=:id",
                id=identifier,
            ):
                raise CatalogError(409, "WV-DRAFT-RETIRED", "Retired drafts cannot be saved")
            current = rows[0]["revision"] if rows else None
            check_revision(current, expected_revision)
            from firefly_weave.compiler.admission import admit_authoring
            from firefly_weave.compiler.schemas import _Failure

            try:
                admit_authoring(
                    document, await repository.snapshot(await self.compiler_capabilities(tx)), incomplete=True
                )
            except _Failure:
                raise CatalogError(
                    422, "WV-DRAFT-CLASSIFICATION", "Draft contains classified or unclassifiable literals"
                ) from None
            draft = Draft(id=identifier, revision=(current or 0) + 1, document=document)
            await repository.execute(
                "INSERT INTO draft_revisions VALUES(:id,:tenant,:project,:revision,cast(:document AS jsonb))",
                id=identifier,
                revision=draft.revision,
                document=json.dumps(document),
            )
            await audit(
                tx.session,
                actor,
                "draft.save",
                str(identifier),
                scope=scope,
                capability="definition.write",
                context=context,
                details={"revision": draft.revision},
            )
            return draft

    async def activate(
        self,
        actor: Principal,
        scope: Scope,
        request: ActivationRequest,
        idempotency_key: str,
        expected_revision: int | None = None,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> Activation:
        if scope != request.scope or scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Activation pins must match the target environment")
        self.require(actor, scope, "release.activate", context)
        async with self.transaction(scope, tx) as tx:
            replay = Idempotency(
                tx,
                actor.id,
                f"activate:{scope.environment_id}",
                idempotency_key,
                {"request": request.model_dump(mode="json"), "expected_revision": expected_revision},
            )
            prior = await replay.replay()
            if prior is not None:
                return Activation.model_validate_json(json.dumps(prior))
            repository = DefinitionRepository(tx)
            row = await repository.version(request.version_id)
            if row["kind"] != "Workflow" or row["digest"] != request.artifact_digest or row["retired"]:
                raise CatalogError(
                    422, "WV-ACTIVATION", "Activation requires an available workflow with the pinned digest"
                )
            try:
                artifact = import_artifact(row["artifact"])
            except UnsupportedIR as error:
                raise ir_unsupported(error.missing) from None
            spec = row["document"]["spec"]
            connector_pins = await self.execution_readiness(
                actor, scope, request, artifact, capability="release.activate", context=context, tx=tx
            )
            from firefly_weave.compiler.ir import HumanTaskNode
            from firefly_weave.human_tasks.service import pin_assignments
            from firefly_weave.runtime.kernel import workflow

            assignment_pins = await pin_assignments(
                tx,
                request.assignment_binding_ids,
                {node.assignment for node in workflow(artifact).graph.nodes if isinstance(node, HumanTaskNode)},
            )
            requirements = spec.get("connections", {})
            required = {slot for slot, requirement in requirements.items() if requirement.get("required", True)}
            supplied_slots = set(request.connection_revision_ids)
            if not required <= supplied_slots or not supplied_slots <= requirements.keys():
                raise CatalogError(422, "WV-CONNECTION", "Activation connection slots do not match requirements")
            for slot, identifier in request.connection_revision_ids.items():
                await self.connections.get().resolve_binding(
                    actor, scope, slot, identifier, requirements[slot]["connector"], context=context, tx=tx
                )
            await lock(tx, f"activation:{scope.tenant_id}:{scope.project_id}:{scope.environment_id}:{row['name']}")
            rows = await repository.rows(
                "SELECT revision FROM activation_revisions WHERE tenant_id=:tenant AND "
                "project_id=:project AND environment_id=:environment AND name=:name ORDER BY revision "
                "DESC LIMIT 1",
                environment=scope.environment_id,
                name=row["name"],
            )
            current = rows[0]["revision"] if rows else None
            check_revision(current, expected_revision)
            activation = Activation(
                id=uuid4(),
                revision=(current or 0) + 1,
                name=row["name"],
                request=request,
                connector_execution_pins=connector_pins,
                assignment_pins=assignment_pins,
            )
            await self._insert_activation(repository, activation)
            for slot, identifier in request.connection_revision_ids.items():
                await repository.execute(
                    "INSERT INTO activation_connections VALUES(:tenant,:project,:environment,"
                    ":activation,:slot,:revision)",
                    environment=scope.environment_id,
                    activation=activation.id,
                    slot=slot,
                    revision=identifier,
                )
            await audit(
                tx.session,
                actor,
                "release.activate",
                str(activation.id),
                scope=scope,
                capability="release.activate",
                context=context,
                details={
                    "version_id": str(request.version_id),
                    "digest": request.artifact_digest,
                    "revision": activation.revision,
                },
            )
            await self.outbox.append(
                tx,
                IntegrationEvent(
                    event_id=uuid5(NAMESPACE_URL, "activation:" + str(activation.id)),
                    type="activation.retired" if activation.retired else "activation.activated",
                    scope=scope,
                    resource_id=activation.id,
                    correlation_id=activation.id,
                    emitted_at=await tx.session.scalar(text("SELECT clock_timestamp()")),
                    payload=EventMetadata(revision=activation.revision),
                ),
            )
            await replay.save(activation.model_dump(mode="json"))
            return activation

    @staticmethod
    async def _insert_activation(repository: DefinitionRepository, activation: Activation) -> None:
        await repository.execute(
            "INSERT INTO activation_revisions "
            "VALUES(:id,:tenant,:project,:environment,:name,:revision,:version,cast(:payload AS "
            "jsonb))",
            id=activation.id,
            environment=activation.request.scope.environment_id,
            name=activation.name,
            revision=activation.revision,
            version=activation.request.version_id,
            payload=activation.model_dump_json(),
        )

        for connector_id, release_id in activation.request.connector_release_ids.items():
            await repository.execute(
                "INSERT INTO activation_connector_releases VALUES(:tenant,:project,:environment,"
                ":activation,:connector,:release)",
                environment=activation.request.scope.environment_id,
                activation=activation.id,
                connector=connector_id,
                release=release_id,
            )

    async def retire(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        kind: DefinitionKind,
        idempotency_key: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> dict[str, Any]:
        scope = project_scope(scope)
        self.require(actor, scope, "release.retire", context)
        async with self.transaction(scope, tx) as tx:
            replay = Idempotency(tx, actor.id, "retire:" + kind, idempotency_key, {"id": str(identifier)})
            prior = await replay.replay()
            if prior is not None:
                return prior
            repository = DefinitionRepository(tx)
            row = await repository.version(identifier)
            if row["kind"] != kind:
                raise CatalogError(404, "WV-NOT-FOUND", "Catalog resource not found")
            await repository.execute(
                "INSERT INTO definition_retirements VALUES(:tenant,:project,:version) ON CONFLICT DO NOTHING",
                version=identifier,
            )
            result = {"id": str(identifier), "retired": True}
            await audit(
                tx.session,
                actor,
                "definition.retire",
                str(identifier),
                scope=scope,
                capability="release.retire",
                context=context,
            )
            await replay.save(result)
            return result

    async def read(
        self,
        actor: Principal,
        scope: Scope,
        collection: str,
        identifier: UUID,
        *,
        export: bool = False,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> dict[str, Any]:
        if collection != "activations":
            scope = project_scope(scope)
        self.require(actor, scope, "catalog.read", context)
        async with self.transaction(scope, tx, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "catalog.read", context)
            repository = DefinitionRepository(tx)
            if collection in {"Workflow", "Action", "Connector", "DecisionTable"}:
                row = await repository.version(identifier)
                if row["kind"] != collection:
                    raise CatalogError(404, "WV-NOT-FOUND", "Catalog resource not found")
                try:
                    import_artifact(row["artifact"])
                except ValueError:
                    raise CatalogError(
                        409,
                        "WV-LEGACY-UNAVAILABLE",
                        "Legacy catalog evidence is unavailable",
                        result={
                            "id": str(identifier),
                            "omissions": [{"path": "", "reason": "classification_unavailable"}],
                        },
                    ) from None
                result = {**published(row).model_dump(mode="json"), "retired": row["retired"]}
                if export:
                    sources = await repository.rows(
                        "SELECT source,format,source_hash,envelope FROM definition_sources WHERE "
                        "tenant_id=:tenant AND project_id=:project AND version_id=:id ORDER BY "
                        "created_at,id",
                        id=identifier,
                    )
                    result.update(document=row["document"], artifact=row["artifact"], sources=sources)
                return result
            if collection == "drafts":
                rows = await repository.rows(
                    "SELECT id,revision,document FROM draft_revisions WHERE tenant_id=:tenant AND "
                    "project_id=:project AND id=:id ORDER BY revision DESC",
                    id=identifier,
                )
                from firefly_weave.compiler.admission import admit_authoring
                from firefly_weave.compiler.schemas import _Failure

                snapshot = await repository.snapshot(await self.compiler_capabilities(tx))
                try:
                    for row in rows:
                        admit_authoring(row["document"], snapshot, incomplete=True)
                except (_Failure, ValueError):
                    raise CatalogError(
                        409,
                        "WV-LEGACY-UNAVAILABLE",
                        "Legacy draft evidence is unavailable",
                        result={
                            "id": str(identifier),
                            "omissions": [{"path": "/document", "reason": "classification_unavailable"}],
                        },
                    ) from None
                retired = await repository.rows(
                    "SELECT revision FROM draft_retirements WHERE tenant_id=:tenant AND project_id=:project AND id=:id",
                    id=identifier,
                )
                values = [
                    DraftView.model_validate(
                        {
                            **row,
                            "retired": bool(retired),
                            "retirement_revision": retired[0]["revision"] if retired else None,
                        }
                    ).model_dump(mode="json")
                    for row in rows
                ]
            elif collection == "activations" and scope.environment_id is not None:
                rows = await repository.rows(
                    "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND "
                    "project_id=:project AND environment_id=:environment AND id=:id",
                    id=identifier,
                    environment=scope.environment_id,
                )
                values = [row["payload"] for row in rows]
            else:
                raise CatalogError(422, "WV-COLLECTION", "Invalid collection")
            if not values:
                raise CatalogError(404, "WV-NOT-FOUND", "Catalog resource not found")
            return {"revisions": values} if export else values[0]

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        collection: str,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "Page limit must be between 1 and 100")
        if collection != "activations":
            scope = project_scope(scope)
        self.require(actor, scope, "catalog.read", context)
        async with self.transaction(scope, tx, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "catalog.read", context)
            repository = DefinitionRepository(tx)
            if collection in {"Workflow", "Action", "Connector", "DecisionTable"}:
                sql = (
                    "SELECT id,kind,name,version,digest,definition_digest,artifact FROM definition_versions "
                    "WHERE tenant_id=:tenant AND project_id=:project AND kind=:kind"
                )
            elif collection == "drafts":
                sql = (
                    "SELECT DISTINCT ON (id) id,revision,document FROM draft_revisions WHERE "
                    "tenant_id=:tenant AND project_id=:project AND NOT EXISTS (SELECT 1 FROM draft_retirements r "
                    "WHERE r.tenant_id=draft_revisions.tenant_id AND r.project_id=draft_revisions.project_id "
                    "AND r.id=draft_revisions.id)"
                )
            elif collection == "activations" and scope.environment_id is not None:
                sql = (
                    "SELECT id,payload FROM activation_revisions WHERE tenant_id=:tenant AND "
                    "project_id=:project AND environment_id=:environment"
                )
            else:
                raise CatalogError(422, "WV-COLLECTION", "Invalid collection")
            sql += (
                (" AND id>:cursor" if cursor is not None else "")
                + " ORDER BY id"
                + (",revision DESC" if collection == "drafts" else "")
                + " LIMIT :limit"
            )
            rows = await repository.rows(
                sql, kind=collection, environment=scope.environment_id, cursor=cursor, limit=limit + 1
            )
            items = []
            for row in rows[:limit]:
                if collection == "drafts":
                    from firefly_weave.compiler.admission import admit_authoring
                    from firefly_weave.compiler.schemas import _Failure

                    try:
                        admit_authoring(
                            row["document"],
                            await repository.snapshot(await self.compiler_capabilities(tx)),
                            incomplete=True,
                        )
                        items.append(Draft.model_validate(row).model_dump(mode="json"))
                    except (_Failure, ValueError):
                        items.append(
                            {
                                "id": str(row["id"]),
                                "unavailable": True,
                                "omissions": [{"path": "/document", "reason": "classification_unavailable"}],
                            }
                        )
                elif collection == "activations":
                    items.append(row["payload"])
                else:
                    try:
                        import_artifact(row["artifact"])
                        items.append(published(row).model_dump(mode="json"))
                    except ValueError:
                        items.append(
                            {
                                "id": str(row["id"]),
                                "unavailable": True,
                                "omissions": [{"path": "", "reason": "classification_unavailable"}],
                            }
                        )
            return {"items": items, "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    async def retire_activation(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        idempotency_key: str,
        expected_revision: int | None,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> Activation:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "An environment is required")
        self.require(actor, scope, "release.retire", context)
        async with self.transaction(scope, tx) as tx:
            replay = Idempotency(
                tx,
                actor.id,
                f"retire-activation:{scope.environment_id}",
                idempotency_key,
                {"id": str(identifier), "expected_revision": expected_revision},
            )
            prior = await replay.replay()
            if prior is not None:
                return Activation.model_validate_json(json.dumps(prior))
            repository = DefinitionRepository(tx)
            rows = await repository.rows(
                "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND "
                "project_id=:project AND environment_id=:environment AND id=:id",
                environment=scope.environment_id,
                id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Activation not found")
            selected = Activation.model_validate_json(json.dumps(rows[0]["payload"]))
            await lock(tx, f"activation:{scope.tenant_id}:{scope.project_id}:{scope.environment_id}:{selected.name}")
            latest = await repository.rows(
                "SELECT id,revision FROM activation_revisions WHERE tenant_id=:tenant AND "
                "project_id=:project AND environment_id=:environment AND name=:name ORDER BY revision "
                "DESC LIMIT 1",
                environment=scope.environment_id,
                name=selected.name,
            )
            check_revision(latest[0]["revision"], expected_revision)
            if latest[0]["id"] != selected.id:
                raise CatalogError(412, "WV-ETAG", "Only the current activation may be retired")
            activation = Activation(
                id=uuid4(),
                revision=selected.revision + 1,
                name=selected.name,
                request=selected.request,
                retired=True,
                connector_execution_pins=selected.connector_execution_pins,
                assignment_pins=selected.assignment_pins,
            )
            await self._insert_activation(repository, activation)
            await audit(
                tx.session,
                actor,
                "release.retire",
                str(activation.id),
                scope=scope,
                capability="release.retire",
                context=context,
                details={"previous_activation_id": str(identifier), "revision": activation.revision},
            )
            await self.outbox.append(
                tx,
                IntegrationEvent(
                    event_id=uuid5(NAMESPACE_URL, "activation:" + str(activation.id)),
                    type="activation.retired" if activation.retired else "activation.activated",
                    scope=scope,
                    resource_id=activation.id,
                    correlation_id=activation.id,
                    emitted_at=await tx.session.scalar(text("SELECT clock_timestamp()")),
                    payload=EventMetadata(revision=activation.revision),
                ),
            )
            await replay.save(activation.model_dump(mode="json"))
            return activation

    async def retire_draft(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        expected_revision: int | None,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> DraftRetirement:
        scope = project_scope(scope)
        self.require(actor, scope, "definition.write", context)
        async with self.transaction(scope, tx) as enlisted:
            await lock(enlisted, f"draft:{scope.tenant_id}:{scope.project_id}:{identifier}")
            self.require(await load_principal(enlisted.session, actor.id), scope, "definition.write", context)
            repository = DefinitionRepository(enlisted)
            rows = await repository.rows(
                "SELECT revision FROM draft_revisions WHERE tenant_id=:tenant AND project_id=:project "
                "AND id=:id ORDER BY revision DESC LIMIT 1",
                id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Draft not found")
            retired = await repository.rows(
                "SELECT revision FROM draft_retirements WHERE tenant_id=:tenant AND project_id=:project AND id=:id",
                id=identifier,
            )
            check_revision(retired[0]["revision"] if retired else rows[0]["revision"], expected_revision)
            if retired:
                raise CatalogError(409, "WV-DRAFT-RETIRED", "Draft is already retired")
            result = DraftRetirement(
                id=identifier, document_revision=rows[0]["revision"], revision=rows[0]["revision"] + 1
            )
            await repository.execute(
                "INSERT INTO draft_retirements(tenant_id,project_id,id,document_revision,revision,principal_id) "
                "VALUES(:tenant,:project,:id,:document_revision,:revision,:principal)",
                **result.model_dump(exclude={"retired"}),
                principal=actor.id,
            )
            await audit(
                enlisted.session,
                actor,
                "draft.retire",
                str(identifier),
                scope=scope,
                capability="definition.write",
                context=context,
                details={"revision": result.revision},
            )
            return result

    async def catalog(self, actor: Principal, scope: Scope, *, context: AuditContext) -> CatalogLock:
        from firefly_weave.contracts.public import catalog_lock

        scope = project_scope(scope)
        self.require(actor, scope, "catalog.read", context)
        async with self.transaction(scope, None, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "catalog.read", context)
            return catalog_lock(await DefinitionRepository(tx).snapshot(await self.compiler_capabilities(tx)))
