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

"""Compilation uses the same pure API and canonical response as offline callers."""

import json
from dataclasses import asdict
from typing import Any

from pydantic import ValidationError
from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.compiler.api import CompileResult
from firefly_weave.contracts.public import (
    CompatibilityAvailability,
    CompilerRequest,
    DebugCapabilities,
    DecisionEvaluationRequest,
    OperationalCapabilities,
    ProcessCapabilities,
    RuntimeCapabilities,
    TransportCapabilities,
)
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.settings import Settings


def compile_payload(result: CompileResult) -> dict[str, Any]:
    return dict(json.loads(result.to_bytes()))


@rest_controller
@request_mapping("")
class CompilerController:
    def __init__(self, service: DefinitionService, settings: Settings) -> None:
        self.service = service
        self.settings = settings

    @operation("compiler.compile")
    async def compile(self, request: Request) -> dict[str, Any]:
        body = CompilerRequest.model_validate_json(await request.body())
        result = await self.service.compile(
            request.state.principal,
            request_scope(request),
            body.source,
            body.format,
            catalog=body.catalog,
            filename=body.filename,
            strict=body.strict,
            context=request.state.audit_context,
        )
        return compile_payload(result)

    @operation("compiler.validate")
    async def validate(self, request: Request) -> dict[str, Any]:
        body = CompilerRequest.model_validate_json(await request.body())
        result = await self.service.compile(
            request.state.principal,
            request_scope(request),
            body.source,
            body.format,
            catalog=body.catalog,
            filename=body.filename,
            strict=body.strict,
            partial=True,
            context=request.state.audit_context,
        )
        return compile_payload(result)

    @operation("compiler.evaluate_decision")
    async def evaluate_decision(self, request: Request) -> dict[str, Any]:
        body = DecisionEvaluationRequest.model_validate_json(await request.body())
        result = await self.service.evaluate_decision(
            request.state.principal, request_scope(request), body, context=request.state.audit_context
        )
        return result.model_dump(mode="json")

    @operation("catalog.read")
    async def catalog(self, request: Request) -> dict[str, Any]:
        result = await self.service.catalog(
            request.state.principal, request_scope(request), context=request.state.audit_context
        )
        return result.model_dump(mode="json", by_alias=True)

    @operation("capabilities.read")
    async def capabilities(self, request: Request) -> dict[str, Any]:
        from firefly_weave.connections.secret_execution import SECRET_SLOTS
        from firefly_weave.contracts.public import Capabilities, compiler_limits
        from firefly_weave.contracts.schema_export import contract_models
        from firefly_weave.operations.compatibility_catalog import WORKER_PROTOCOL
        from firefly_weave.operations.debug.models import (
            DEBUG_SESSION_RESERVATION_BYTES,
            DEBUG_SESSIONS_PER_CREATOR,
            DEBUG_SESSIONS_PER_PROJECT,
            DebugLimits,
        )
        from firefly_weave.operations.execution import CONTROL_SLOTS, DEBUG_EXECUTION_SLOTS, WORK_SLOTS
        from firefly_weave.operations.transport import (
            BODY_BYTES,
            BODY_SLOTS,
            CONTROL_BODY_BYTES,
            CONTROL_BODY_SLOTS,
            DEBUG_SLOTS,
            TransportPolicy,
        )
        from firefly_weave.runtime.capacity import MAX_INTENTS, MAX_REDUCTIONS, STATE_BYTES, TRANSITION_BYTES

        await self.service.catalog(request.state.principal, request_scope(request), context=request.state.audit_context)
        result = Capabilities(
            limits=compiler_limits(),
            schemas=sorted(contract_models()),
            connectors=sorted(r.reference for r in self.service.capabilities.resources.values() if r.kind == "Adapter"),
            operations=OperationalCapabilities.model_validate(
                dict(
                    policy=self.settings.operations,
                    policy_fingerprint=self.settings.operations.fingerprint,
                    worker_protocol_versions=[WORKER_PROTOCOL],
                    transport=TransportCapabilities(**asdict(TransportPolicy())),
                    runtime=RuntimeCapabilities(
                        state_bytes=STATE_BYTES,
                        transition_bytes=TRANSITION_BYTES,
                        reductions_per_turn=MAX_REDUCTIONS,
                        intents_per_turn=MAX_INTENTS,
                    ),
                    process=ProcessCapabilities(
                        work_slots=WORK_SLOTS,
                        control_slots=CONTROL_SLOTS,
                        secret_slots=SECRET_SLOTS,
                        body_slots=BODY_SLOTS,
                        body_bytes=BODY_BYTES,
                        control_body_slots=CONTROL_BODY_SLOTS,
                        control_body_bytes=CONTROL_BODY_BYTES,
                        debug_body_slots=DEBUG_SLOTS,
                        debug_execution_slots=DEBUG_EXECUTION_SLOTS,
                    ),
                    debug=DebugCapabilities(
                        session_bytes=DebugLimits().session_bytes,
                        lifetime_seconds=DebugLimits().lifetime_seconds,
                        sessions_per_creator=DEBUG_SESSIONS_PER_CREATOR,
                        sessions_per_project=DEBUG_SESSIONS_PER_PROJECT,
                        session_reservation_bytes=DEBUG_SESSION_RESERVATION_BYTES,
                    ),
                    compatibility=_compatibility_availability(request),
                )
            ),
        )
        return result.model_dump(mode="json")

    @operation("language.read")
    async def language(self, request: Request) -> dict[str, Any]:
        from firefly_weave.contracts.language import language_manifest

        await self.service.catalog(request.state.principal, request_scope(request), context=request.state.audit_context)
        return language_manifest().model_dump(mode="json")

    @operation("schemas.read")
    async def schemas(self, request: Request) -> dict[str, Any]:
        from firefly_weave.contracts.schema_export import export_schemas

        await self.service.catalog(request.state.principal, request_scope(request), context=request.state.audit_context)
        return export_schemas()


def _compatibility_availability(request: Request) -> CompatibilityAvailability:
    # Missing startup state never means ready; project metadata must not leak inventory details.
    try:
        state = request.app.state.compatibility
        report = state.report
        return CompatibilityAvailability(
            ready=state.ready,
            mode=report.mode,
            complete=report.complete,
            checked_at=report.checked_at,
        )
    except (AttributeError, KeyError, ValidationError):
        return CompatibilityAvailability()
