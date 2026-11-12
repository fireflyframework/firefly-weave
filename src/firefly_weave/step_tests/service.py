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
"""Running one step of a saved draft for real: the checks, in order, then one transaction that starts its test run.

Every refusal comes before anything is written. Only the person who started a step test can read or cancel it;
anyone else is told it doesn't exist.
"""

import copy
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.run_views import RunStepQuery
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.step_tests import (
    DraftStep,
    StepTestAccepted,
    StepTestRequest,
    StepTestView,
    SynthesizedTest,
)
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService, project_scope
from firefly_weave.operations.fact_reads import FactReadRepository, step_of
from firefly_weave.operations.facts import RunStartFacts
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.step_tests.admission import (
    ReleaseChoices,
    busy_refusal,
    confirmation_needed,
    draft_step,
    input_refusal,
    release_pins,
    start_audit,
)
from firefly_weave.step_tests.policy import TestPolicyService
from firefly_weave.step_tests.synthesis import StepTestSynthesizers, test_version
from firefly_weave.step_tests.view import StepTestDetails, step_test_view

# A step test's activation outlives its run's timeout by this much, so its result can still be read.
GRACE = timedelta(seconds=60)


def _not_found() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "Step test not found")


def _mismatch(message: str = "The saved draft's step doesn't match this request; save and try again") -> CatalogError:
    return CatalogError(422, "WV-STEPTEST-KIND", message)


def _connection(slots: dict[str, str]) -> CatalogError:
    return CatalogError(
        422, "WV-STEPTEST-CONNECTION", "A connection for this step can't be used", result={"slots": slots}
    )


@dataclass(frozen=True)
class Admitted:
    draft: DraftStep
    synthesized: SynthesizedTest
    workflow: JsonObject
    artifact: CompiledArtifact
    worker_release_ids: dict[str, UUID]
    connector_release_ids: dict[UUID, UUID]


@service
class StepTestService:
    def __init__(
        self,
        uow: UnitOfWork,
        authorization: AuthorizationService,
        definitions: DefinitionService,
        connections: ConnectionService,
        runtime: RuntimeService,
        policy: TestPolicyService,
        synthesizers: StepTestSynthesizers,
    ) -> None:
        self.uow, self.authorization, self.definitions = uow, authorization, definitions
        self.connections, self.runtime, self.policy, self.synthesizers = connections, runtime, policy, synthesizers

    async def admit(
        self,
        actor: Principal,
        scope: Scope,
        request: StepTestRequest,
        activation_id: UUID,
        *,
        context: AuditContext,
        tx: Transaction,
    ) -> Admitted:
        """Every check a step test passes before anything is written, in this order."""
        self.authorization.require(actor, scope, "step.test", context=context)
        # The draft is a project resource: who can't read it can't run its steps.
        self.authorization.require(actor, project_scope(scope), "catalog.read", context=context)
        if not await self.policy.enabled(tx):
            raise CatalogError(403, "WV-STEPTEST-DISABLED", "Test calls are off in this environment")
        self.definitions.registry.require_operational()
        from firefly_weave.step_tests.repository import StepTestRepository

        repository = StepTestRepository(tx)
        workflow = await repository.draft(request.draft_id, request.draft_revision)
        if workflow is None:
            raise CatalogError(404, "WV-NOT-FOUND", "Draft not found")
        step = draft_step(workflow, request.step_id)
        if step is None:
            raise CatalogError(404, "WV-NOT-FOUND", "The saved draft has no such step")
        if (
            step.get("kind") != request.kind
            or step.get("uses") != request.uses
            or (request.kind == "llm" and step.get("profile") != request.profile)
        ):
            raise _mismatch()
        synthesizer = self.synthesizers.for_kind(request.kind)
        if synthesizer is None:
            raise _mismatch("Steps of this kind can't run for real")
        if request.kind != "agent" and (request.tool_mocks or request.real_tools):
            raise _mismatch("Only AI agent steps take tool mocks or real tools")
        catalog = await DefinitionRepository(tx).snapshot(await self.definitions.compiler_capabilities(tx))
        draft = DraftStep(workflow, step)
        synthesized = synthesizer.synthesize(request, draft, catalog)

        current = await load_principal(tx.session, actor.id)
        for chosen in request.connections.values():
            self.connections.require_revision(actor, current, scope, chosen, "connection.bind", context)
        declared = cast(JsonObject, cast(JsonObject, synthesized.workflow["spec"])["connections"])
        missing = sorted(slot for slot in declared if slot not in synthesized.slots)
        if missing:
            raise _connection({slot: f"Choose a connection for slot {slot}." for slot in missing})

        uses = cast(str, step["uses"])
        if confirmation_needed(synthesized.side_effect, request.acknowledged_side_effect):
            raise CatalogError(
                409,
                "WV-STEPTEST-CONFIRM",
                "This step changes data; confirm its effect to run it for real",
                result={
                    "side_effect": synthesized.side_effect,
                    "action": uses,
                    "environment": await repository.environment_name(),
                },
            )

        schemas = {name: document.value for name, document in catalog.schemas.items()}
        refused = input_refusal(
            cast(JsonObject, cast(JsonObject, synthesized.workflow["spec"])["inputSchema"]), synthesized.input, schemas
        )
        if refused is not None:
            raise refused

        problems: dict[str, str] = {}
        for slot, revision_id in synthesized.slots.items():
            try:
                revision = await ConnectionRepository(tx).revision(revision_id)
            except CatalogError:
                problems[slot] = "This connection isn't available in this environment."
                continue
            if revision.connector != cast(JsonObject, declared[slot])["connector"]:
                problems[slot] = "This connection is for another connector."
                continue
            try:
                await self.connections._ready(
                    current, scope, revision, "connection.bind", context, tx, explain=True, resource=str(revision_id)
                )
            except CatalogError as error:
                diagnostics = (error.result or {}).get("diagnostics") if isinstance(error.result, dict) else None
                first = diagnostics[0] if isinstance(diagnostics, list) and diagnostics else None
                problems[slot] = (
                    str(first["message"])
                    if isinstance(first, dict) and "message" in first
                    else "This connection can't be used as configured."
                )
        if problems:
            raise _connection(problems)

        final = copy.deepcopy(synthesized.workflow)
        metadata = cast(JsonObject, final["metadata"])
        metadata["version"] = test_version(cast(str, metadata["version"]), activation_id)
        compiled = compile_source(
            final, format="object", catalog=catalog, action_validators=self.definitions.registry.action_validators()
        )
        if not compiled.ok or compiled.artifact is None:
            raise _mismatch("This step can't run on its own as saved; save the draft and try again")
        needed = await repository.release_choices(compiled.artifact)
        pinned = release_pins(needed, {str(key): value for key, value in request.releases.items()})
        if isinstance(pinned, ReleaseChoices):
            raise CatalogError(422, "WV-STEPTEST-RELEASE", pinned.message(), result=pinned.result())
        executable = compiled.artifact.executable
        worker_release_ids: dict[str, UUID] = {}
        connector_release_ids: dict[UUID, UUID] = {}
        for reference, release in pinned.pins.items():
            connector = next(
                (
                    d
                    for d in cast(list[JsonObject], executable["dependencies"])
                    if d["kind"] == "Connector" and d["reference"] == reference
                ),
                None,
            )
            if connector is None:
                worker_release_ids[reference.partition("@")[0]] = release
                continue
            version = await repository.connector_version(cast(str, connector["digest"]))
            if version is None:
                raise CatalogError(422, "WV-STEPTEST-RELEASE", f"No connector version here matches {reference}.")
            connector_release_ids[version] = release
        return Admitted(draft, synthesized, final, compiled.artifact, worker_release_ids, connector_release_ids)

    async def start(
        self,
        actor: Principal,
        scope: Scope,
        request: StepTestRequest,
        idempotency_key: str,
        *,
        context: AuditContext,
    ) -> StepTestAccepted:
        """Write the synthetic version, the test activation and its details, and start the test run, all at once."""
        from firefly_weave.step_tests.repository import StepTestRepository

        # A stored answer goes only to someone who may still test steps here, as a run start's replay does.
        self.authorization.require(actor, scope, "step.test", context=context)
        async with self.uow.open(scope) as tx:
            replay = Idempotency(
                tx,
                actor.id,
                f"step_test.start:{scope.environment_id}",
                idempotency_key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return StepTestAccepted.model_validate_json(json.dumps(prior))
            activation_id = uuid4()
            admitted = await self.admit(actor, scope, request, activation_id, context=context, tx=tx)
            repository = StepTestRepository(tx)
            # Concurrent starts by the same person in this environment count each other.
            await lock(tx, f"step-test:{actor.id}:{scope.environment_id}")
            busy = busy_refusal(*await repository.busy(actor.id, await repository.now()), await repository.now())
            if busy is not None:
                raise busy
            version_id = uuid4()
            definition_digest = FrozenDocument.from_value(
                load_definition(admitted.workflow).model_dump(by_alias=True)
            ).digest
            await repository.insert_version(version_id, admitted.workflow, admitted.artifact, definition_digest)
            activation_request = ActivationRequest(
                scope=scope,
                version_id=version_id,
                artifact_digest=admitted.artifact.digest,
                connection_revision_ids=admitted.synthesized.slots,
                connector_release_ids=admitted.connector_release_ids,
                worker_release_ids=admitted.worker_release_ids,
            )
            pins = await self.definitions.execution_readiness(
                actor, scope, activation_request, admitted.artifact, capability="step.test", context=context, tx=tx
            )
            activation = Activation(
                id=activation_id,
                revision=1,
                name="test-" + activation_id.hex[:12],
                request=activation_request,
                connector_execution_pins=pins,
            )
            await repository.insert_activation(activation)
            now = await repository.now()
            expires_at = now + timedelta(seconds=admitted.synthesized.timeout_seconds) + GRACE
            await repository.insert_details(
                activation_id,
                actor.id,
                draft=request.draft_id,
                draft_revision=request.draft_revision,
                step_id=request.step_id,
                side_effect=admitted.synthesized.side_effect,
                acknowledged=request.acknowledged_side_effect,
                created_at=now,
                expires_at=expires_at,
            )
            run = await self.runtime.start(
                actor,
                scope,
                StartRunRequest(activation_id=activation_id, input=admitted.synthesized.input),
                f"step-test:{activation_id}",
                context=context,
                tx=tx,
                start_facts=RunStartFacts(origin="test", test=True),
                capability="step.test",
                test_activation=True,
            )
            await repository.record_run(activation_id, run.id)
            await audit(
                tx.session,
                actor,
                "step_test.start",
                str(activation_id),
                scope=scope,
                capability="step.test",
                context=context.model_copy(update={"run_id": run.id}),
                details=start_audit(request, admitted.synthesized.side_effect, run.id),
            )
            accepted = StepTestAccepted(
                id=activation_id,
                run_id=run.id,
                status="running",
                side_effect=admitted.synthesized.side_effect,
                expires_at=expires_at,
            )
            await replay.save(accepted.model_dump(mode="json"))
            return accepted

    async def _owned(self, actor: Principal, scope: Scope, identifier: UUID, tx: Transaction) -> dict[str, Any]:
        from firefly_weave.step_tests.repository import StepTestRepository

        details = await StepTestRepository(tx).details(identifier)
        # Anyone but its creator, administrators included, is told it doesn't exist.
        if details is None or details["principal_id"] != actor.id or details["run_id"] is None:
            raise _not_found()
        return details

    async def _view(self, tx: Transaction, details: dict[str, Any]) -> StepTestView:
        from firefly_weave.step_tests.repository import StepTestRepository

        run_id = cast(UUID, details["run_id"])
        status = await StepTestRepository(tx).run_status(run_id) or "running"
        facts = FactReadRepository(tx)
        rows, _ = await facts.step_rows(run_id, RunStepQuery(step=details["step_id"], limit=1), None)
        step = step_of(rows[0]) if rows else None
        outputs = await facts.outputs(run_id, rows) if rows and status == "succeeded" else {}
        output = outputs.get(rows[0]["instance_key"] or rows[0]["node_id"]) if rows else None
        return step_test_view(
            StepTestDetails(details["activation_id"], run_id, details["side_effect"], details["created_at"]),
            status,
            step,
            output,
        )

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> StepTestView:
        self.authorization.require(actor, scope, "step.test", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            return await self._view(tx, await self._owned(actor, scope, identifier, tx))

    async def cancel(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> StepTestView:
        """Cancel a running step test; one that already ended answers as it ended."""
        from firefly_weave.step_tests.repository import StepTestRepository

        self.authorization.require(actor, scope, "step.test", context=context)
        async with self.uow.open(scope) as tx:
            details = await self._owned(actor, scope, identifier, tx)
            status = await StepTestRepository(tx).run_status(cast(UUID, details["run_id"]))
            if status not in {"succeeded", "failed", "cancelled", "timed_out"}:
                await self.runtime.cancel(
                    tx,
                    cast(UUID, details["run_id"]),
                    "Canceled from step details",
                    actor=actor,
                    scope=scope,
                    context=context,
                    capability="step.test",
                )
                await audit(
                    tx.session,
                    actor,
                    "step_test.cancel",
                    str(identifier),
                    scope=scope,
                    capability="step.test",
                    context=context,
                    details={"run_id": str(details["run_id"])},
                )
            return await self._view(tx, details)
