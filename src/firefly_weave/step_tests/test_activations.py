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

"""Draft test activations: a short-lived activation of a saved draft, so Execute workflow can run it for real.

Creating one passes the checks a step test passes, for the whole draft: the environment's test policy, binding every
connection it names, and acknowledging the strongest side effect of every step, agent tool and pinned call tree.
It is a test activation, so no list, lookup or call finds it, and it expires. Only its creator may run it, listen
through it or retire it; anyone else is told it doesn't exist. Every refusal comes before anything is written, and
audits record identities and effects, never values.
"""

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import CompileResult, compile_source
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.ir import ActionNode, AgentNode, CallWorkflowNode, HumanTaskNode, WorkflowIR
from firefly_weave.connections.secrets import ScopedSecrets
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest
from firefly_weave.contracts.definitions import SideEffect, load_definition
from firefly_weave.contracts.runtime import RunView, StartRunRequest
from firefly_weave.contracts.step_tests import (
    DRAFT_TEST_MAX_LIFETIME_SECONDS,
    DraftTestActivation,
    DraftTestActivationRequest,
    DraftTestListen,
    DraftTestListenRequest,
    DraftTestRunRequest,
)
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.ai import validate_ai_bindings, validate_ai_reserved
from firefly_weave.definitions.calls import (
    SIDE_EFFECTS,
    call_pins,
    call_tree_side_effect,
    pinned_call_tree,
    pinned_callee,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService, project_scope
from firefly_weave.human_tasks.service import pin_assignments
from firefly_weave.operations.facts import RunStartFacts
from firefly_weave.persistence.idempotency import lock
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.agent_tools import assignment_slots
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.step_tests.admission import busy_refusal, confirmation_needed
from firefly_weave.step_tests.policy import TestPolicyService
from firefly_weave.step_tests.repository import StepTestRepository
from firefly_weave.step_tests.synthesis import test_version
from firefly_weave.triggers.models import Trigger


def draft_lifetime(requested: int, executable: JsonObject) -> int:
    """How long a draft test activation lives: as asked, but never shorter than the draft's longest step timeout.

    A draft whose steps may take more than the longest lifetime can't run as a test activation.
    """
    longest = max(
        (
            cast(int, cast(JsonObject, cast(JsonObject, dependency["document"])["spec"]).get("timeoutSeconds", 0))
            for dependency in cast(list[JsonObject], executable.get("dependencies", []))
            if dependency["kind"] == "Action"
        ),
        default=0,
    )
    if longest > DRAFT_TEST_MAX_LIFETIME_SECONDS:
        raise CatalogError(
            422,
            "WV-TESTACTIVATION-LIFETIME",
            f"A step of this draft may take longer than {DRAFT_TEST_MAX_LIFETIME_SECONDS // 60} minutes, "
            "longer than a test activation lives",
        )
    return max(requested, longest)


def node_uses(ir: WorkflowIR) -> dict[str, list[str]]:
    """The dependencies each step runs, by digest: its action, its agent's tools, or the workflow it calls."""
    uses: dict[str, list[str]] = {}
    for node in ir.graph.nodes:
        if isinstance(node, ActionNode | CallWorkflowNode):
            uses[node.id] = [node.dependency]
        elif isinstance(node, AgentNode):
            uses[node.id] = [tool.dependency for tool in node.tools if tool.dependency is not None]
    return uses


def step_effects(
    uses: Mapping[str, Sequence[str]], executable: JsonObject, callee_effects: Mapping[str, SideEffect]
) -> dict[str, SideEffect]:
    """Each step's strongest side effect: its actions' declared effects, and a called workflow's whole tree's.

    ``callee_effects`` holds the strongest effect of each pinned call tree, by the called reference.
    """
    dependencies = {
        cast(str, dependency["digest"]): dependency
        for dependency in cast(list[JsonObject], executable.get("dependencies", []))
    }
    effects: dict[str, SideEffect] = {}
    for step, digests in uses.items():
        strongest: SideEffect = SIDE_EFFECTS[0]
        for digest in digests:
            dependency = dependencies.get(digest)
            if dependency is None:
                continue
            if dependency["kind"] == "Action":
                spec = cast(JsonObject, cast(JsonObject, dependency["document"])["spec"])
                effect = cast(SideEffect, spec["sideEffect"])
            elif dependency["kind"] == "Workflow":
                effect = callee_effects.get(cast(str, dependency["reference"]), SIDE_EFFECTS[0])
            else:
                continue
            strongest = max(strongest, effect, key=SIDE_EFFECTS.index)
        effects[step] = strongest
    return effects


def confirmation_refusal(
    effect: SideEffect, effects: Mapping[str, SideEffect], acknowledged: SideEffect | None
) -> CatalogError | None:
    """409 when the draft changes data and the person didn't acknowledge exactly that; it names the steps that do."""
    if not confirmation_needed(effect, acknowledged):
        return None
    return CatalogError(
        409,
        "WV-STEPTEST-CONFIRM",
        "This workflow changes data; confirm its effect to run it for real",
        result={"side_effect": effect, "steps": sorted(step for step, made in effects.items() if made == effect)},
    )


def synthetic_document(draft: JsonObject, activation_id: UUID) -> JsonObject:
    """The draft as this test activation's own version: marked test, and its runs never longer than an hour."""
    document = copy.deepcopy(draft)
    metadata = cast(JsonObject, document["metadata"])
    metadata["version"] = test_version(cast(str, metadata["version"]), activation_id)
    spec = cast(JsonObject, document["spec"])
    spec["timeoutSeconds"] = min(cast(int, spec.get("timeoutSeconds", DRAFT_TEST_MAX_LIFETIME_SECONDS)), 3600)
    return document


def compile_diagnostics(result: CompileResult) -> list[dict[str, str]]:
    """What a refused compile shows: each problem's code, place and message, never a source excerpt."""
    return [{"code": d.code, "severity": d.severity, "path": d.path, "message": d.message} for d in result.diagnostics][
        :50
    ]


def not_found() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "Test activation not found")


def expired() -> CatalogError:
    return CatalogError(410, "WV-TESTACTIVATION-EXPIRED", "This test activation has expired")


def uses_ai(nodes: Iterable[object]) -> bool:
    return any(
        isinstance(node, AgentNode) or (isinstance(node, ActionNode) and node.llm_profile is not None) for node in nodes
    )


@service
class TestActivationService:
    __test__ = False

    def __init__(
        self,
        uow: UnitOfWork,
        authorization: AuthorizationService,
        definitions: DefinitionService,
        connections: ConnectionService,
        runtime: RuntimeService,
        policy: TestPolicyService,
        secrets: ScopedSecrets,
    ) -> None:
        self.uow, self.authorization, self.definitions = uow, authorization, definitions
        self.connections, self.runtime, self.policy, self.secrets = connections, runtime, policy, secrets

    async def create(
        self, actor: Principal, scope: Scope, request: DraftTestActivationRequest, *, context: AuditContext
    ) -> DraftTestActivation:
        """Write the draft's test version and its test activation, after every check, in one transaction."""
        self.authorization.require(actor, scope, "step.test", context=context)
        # The draft is a project resource: who can't read it can't run it.
        self.authorization.require(actor, project_scope(scope), "catalog.read", context=context)
        async with self.uow.open(scope) as tx:
            if not await self.policy.enabled(tx):
                raise CatalogError(403, "WV-STEPTEST-DISABLED", "Test calls are off in this environment")
            self.definitions.registry.require_operational()
            repository = StepTestRepository(tx)
            draft = await repository.draft(request.draft_id, request.draft_revision)
            if draft is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Draft not found")
            if await repository.latest_draft_revision(request.draft_id) != request.draft_revision:
                raise CatalogError(409, "WV-REVISION", "The draft changed; save it and try again")
            if draft.get("kind") != "Workflow":
                raise CatalogError(422, "WV-TESTACTIVATION-COMPILE", "Only a workflow draft runs as a test activation")
            bindings = request.bindings
            current = await load_principal(tx.session, actor.id)
            for revision in bindings.connection_revision_ids.values():
                self.connections.require_revision(actor, current, scope, revision, "connection.bind", context)

            activation_id = uuid4()
            document = synthetic_document(draft, activation_id)
            definitions = DefinitionRepository(tx)
            catalog = await definitions.snapshot(await self.definitions.compiler_capabilities(tx))
            compiled = compile_source(
                document,
                format="object",
                catalog=catalog,
                action_validators=self.definitions.registry.action_validators(),
            )
            if not compiled.ok or compiled.artifact is None:
                raise CatalogError(
                    422,
                    "WV-TESTACTIVATION-COMPILE",
                    "The saved draft doesn't compile; fix it and try again",
                    result={"diagnostics": compile_diagnostics(compiled)},
                )
            artifact = compiled.artifact
            executable = artifact.executable
            ir = workflow(artifact)
            resolve = pinned_call_tree(definitions, scope)
            callee_effects: dict[str, SideEffect] = {}
            for reference, identifier in bindings.workflow_activation_ids.items():
                callee, pins = await resolve(identifier)
                callee_effects[reference] = await call_tree_side_effect(callee, pins, resolve)
            effect = await call_tree_side_effect(executable, bindings.workflow_activation_ids, resolve)
            effects = step_effects(node_uses(ir), executable, callee_effects)
            refusal = confirmation_refusal(effect, effects, request.acknowledged_side_effect)
            if refusal is not None:
                raise refusal
            lifetime = draft_lifetime(request.lifetime_seconds, executable)

            # Concurrent creates and runs by the same person in this environment count each other.
            await lock(tx, f"step-test:{actor.id}:{scope.environment_id}")
            now = await repository.now()
            busy = busy_refusal([], (await repository.busy(actor.id, now))[1], now)
            if busy is not None:
                raise busy
            version_id = uuid4()
            activation_request = ActivationRequest(
                scope=scope,
                version_id=version_id,
                artifact_digest=artifact.digest,
                connection_revision_ids=bindings.connection_revision_ids,
                connector_release_ids=bindings.connector_release_ids,
                worker_release_ids=bindings.worker_release_ids,
                assignment_binding_ids=bindings.assignment_binding_ids,
                workflow_activation_ids=bindings.workflow_activation_ids,
            )
            definition_digest = FrozenDocument.from_value(load_definition(document).model_dump(by_alias=True)).digest
            await repository.insert_version(version_id, document, artifact, definition_digest)
            connector_pins = await self.definitions.execution_readiness(
                actor, scope, activation_request, artifact, capability="step.test", context=context, tx=tx
            )
            name = cast(str, cast(JsonObject, document["metadata"])["name"])
            workflow_call_pins, call_depth = await call_pins(
                name,
                scope,
                executable,
                bindings.workflow_activation_ids,
                lambda identifier: pinned_callee(definitions, scope, identifier),
            )
            nodes = ir.graph.nodes
            assignment_pins = await pin_assignments(
                tx,
                bindings.assignment_binding_ids,
                {node.assignment for node in nodes if isinstance(node, HumanTaskNode)} | assignment_slots(ir),
            )
            requirements = cast(dict[str, JsonObject], cast(JsonObject, document["spec"]).get("connections", {}))
            required = {slot for slot, need in requirements.items() if need.get("required", True)}
            supplied = set(bindings.connection_revision_ids)
            if not required <= supplied or not supplied <= requirements.keys():
                raise CatalogError(
                    422,
                    "WV-STEPTEST-CONNECTION",
                    "The connections don't match the draft's slots",
                    result={
                        "slots": {slot: f"Choose a connection for slot {slot}." for slot in sorted(required - supplied)}
                        | {slot: "The draft has no such slot." for slot in sorted(supplied - requirements.keys())}
                    },
                )
            bound = {
                slot: await self.connections.resolve_binding(
                    actor, scope, slot, revision, cast(str, requirements[slot]["connector"]), context=context, tx=tx
                )
                for slot, revision in bindings.connection_revision_ids.items()
            }
            validate_ai_reserved(ir)
            if uses_ai(nodes):
                validate_ai_bindings(nodes, bound, self.definitions.ai_policy.current())
            activation = Activation(
                id=activation_id,
                revision=1,
                name="test-" + activation_id.hex[:12],
                request=activation_request,
                connector_execution_pins=connector_pins,
                assignment_pins=assignment_pins,
                workflow_call_pins=workflow_call_pins,
                call_depth=call_depth,
            )
            await repository.insert_activation(activation)
            expires_at = now + timedelta(seconds=lifetime)
            await repository.insert_details(
                activation_id,
                actor.id,
                draft=request.draft_id,
                draft_revision=request.draft_revision,
                step_id=None,
                side_effect=effect,
                acknowledged=request.acknowledged_side_effect,
                created_at=now,
                expires_at=expires_at,
                source="draft",
            )
            await audit(
                tx.session,
                actor,
                "test_activation.create",
                str(activation_id),
                scope=scope,
                capability="step.test",
                context=context,
                details={
                    "draft_id": str(request.draft_id),
                    "draft_revision": request.draft_revision,
                    "side_effect": effect,
                    "acknowledged_side_effect": request.acknowledged_side_effect,
                    "connection_revision_ids": sorted(str(v) for v in bindings.connection_revision_ids.values()),
                    "workflow_activation_ids": sorted(str(v) for v in bindings.workflow_activation_ids.values()),
                    "expires_at": expires_at.isoformat(),
                },
            )
            return DraftTestActivation(
                id=activation_id,
                activation_id=activation_id,
                artifact_digest=artifact.digest,
                side_effect=effect,
                expires_at=expires_at,
            )

    async def _owned(
        self, actor: Principal, identifier: UUID, repository: StepTestRepository, *, lock: bool = False
    ) -> dict[str, Any]:
        details = await repository.details(identifier, "draft", lock=lock)
        # Anyone but its creator, administrators included, is told it doesn't exist.
        if details is None or details["principal_id"] != actor.id:
            raise not_found()
        return details

    # Named start, not run: the container treats any bean with a run method as a command-line runner.
    async def start(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: DraftTestRunRequest,
        idempotency_key: str | None,
        *,
        context: AuditContext,
    ) -> RunView:
        """Start a test run of the activation with this input; it needs step.test, not run.start."""
        self.authorization.require(actor, scope, "step.test", context=context)
        async with self.uow.open(scope) as tx:
            repository = StepTestRepository(tx)
            details = await self._owned(actor, identifier, repository)
            now = await repository.now()
            if details["retired_at"] is not None or details["expires_at"] <= now:
                raise expired()
            await lock(tx, f"step-test:{actor.id}:{scope.environment_id}")
            busy = busy_refusal(*await repository.busy(actor.id, now), now)
            if busy is not None:
                raise busy
            run = await self.runtime.start(
                actor,
                scope,
                StartRunRequest(activation_id=identifier, input=request.input),
                f"test-activation:{identifier}:{idempotency_key or uuid4()}",
                context=context,
                tx=tx,
                start_facts=RunStartFacts(origin="test", test=True),
                capability="step.test",
                test_activation=True,
            )
            await audit(
                tx.session,
                actor,
                "test_activation.run",
                str(identifier),
                scope=scope,
                capability="step.test",
                context=context.model_copy(update={"run_id": run.id}),
                details={"run_id": str(run.id)},
            )
            return run

    async def listen(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: DraftTestListenRequest,
        base_url: str,
        *,
        context: AuditContext,
    ) -> DraftTestListen:
        """A signed webhook route on the test activation; it stops when the activation is retired or expires.

        Listening again answers the route the activation already has.
        """
        self.authorization.require(actor, scope, "step.test", context=context)
        self.authorization.require(actor, scope, "trigger.manage", context=context)
        async with self.uow.open(scope) as tx:
            repository = StepTestRepository(tx)
            details = await self._owned(actor, identifier, repository, lock=True)
            if details["retired_at"] is not None or details["expires_at"] <= await repository.now():
                raise expired()
            self.secrets.check(scope, request.secret_ref)
            route = await repository.listen_route(identifier)
            if route is None:
                trigger = Trigger(
                    id=uuid4(),
                    principal_id=actor.id,
                    name="test-" + identifier.hex[:12],
                    kind="run",
                    activation_id=identifier,
                    secret_ref=request.secret_ref,
                    max_body_bytes=request.max_body_bytes,
                    tolerance_seconds=request.tolerance_seconds,
                )
                await repository.execute(
                    "INSERT INTO trigger_routes(id,tenant_id,project_id,environment_id,principal_id,activation_id,"
                    "secret_ref,tolerance_seconds,max_body_bytes,payload) VALUES(:id,:tenant,:project,:environment,"
                    ":principal,:activation,:secret,:tolerance,:max_body,cast(:payload AS jsonb))",
                    id=trigger.id,
                    principal=actor.id,
                    activation=identifier,
                    secret=trigger.secret_ref,
                    tolerance=trigger.tolerance_seconds,
                    max_body=trigger.max_body_bytes,
                    payload=json.dumps(trigger.model_dump(mode="json")),
                )
                route = trigger.id
                await audit(
                    tx.session,
                    actor,
                    "test_activation.listen",
                    str(identifier),
                    scope=scope,
                    capability="trigger.manage",
                    context=context,
                    details={"trigger_id": str(route)},
                )
            test_url = f"{base_url.rstrip('/')}/webhooks/{route}"
            return DraftTestListen(test_url=test_url, expires_at=details["expires_at"])

    async def retire(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> None:
        """Retire the activation now: its runs refuse new starts and its listen route stops; started runs go on."""
        self.authorization.require(actor, scope, "step.test", context=context)
        async with self.uow.open(scope) as tx:
            repository = StepTestRepository(tx)
            details = await self._owned(actor, identifier, repository, lock=True)
            if details["retired_at"] is not None:
                return
            await repository.retire(identifier, await repository.now())
            await audit(
                tx.session,
                actor,
                "test_activation.retire",
                str(identifier),
                scope=scope,
                capability="step.test",
                context=context,
            )
