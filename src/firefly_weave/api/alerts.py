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

"""Alert operations: rules, previews, presets, channels, settings, opt-ins and each person's own alert address.

Every operation is authorized by its service. The person an alert address belongs to is always the signed-in one.
"""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import (
    decode_cursor_v2,
    etag,
    page_request,
    page_response,
    parse_query,
    parse_revision,
)
from firefly_weave.contracts.alerts import (
    AlertChannel,
    AlertChannelDraft,
    AlertEventQuery,
    AlertQuery,
    AlertRecipientConsentDraft,
    AlertRuleDraft,
    AlertRuleView,
    AlertSettings,
    AlertSettingsUpdate,
    MyAlertAddressUpdate,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_channels import (
    AlertAddressService,
    AlertChannelService,
    AlertRecipientConsentService,
    AlertSettingsService,
)
from firefly_weave.operations.alert_rules import AlertRuleService, AlertService
from firefly_weave.operations.fact_positions import run_position
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.retention_periods import RetentionPeriodService
from firefly_weave.settings import Settings


def _revision(request: Request) -> int | None:
    """The If-Match revision, if sent; a missing one is refused by the service after authorization."""
    try:
        return parse_revision(request.headers.get("If-Match"))
    except ValueError:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision") from None


def _tagged(record: AlertRuleView | AlertChannel, request: Request, status: int = 200) -> JSONResponse:
    return JSONResponse(
        record.model_dump(mode="json"), status_code=status, headers={"ETag": etag(record.revision, request.url.path)}
    )


def _settings(settings: AlertSettings, request: Request) -> JSONResponse:
    headers = {"ETag": etag(settings.revision, request.url.path)} if settings.revision else {}
    return JSONResponse(settings.model_dump(mode="json"), headers=headers)


@rest_controller
@request_mapping("")
class AlertController:
    def __init__(
        self,
        rules: AlertRuleService,
        channels: AlertChannelService,
        settings: AlertSettingsService,
        consents: AlertRecipientConsentService,
        addresses: AlertAddressService,
        alerts: AlertService,
        retention: RetentionPeriodService,
        configured: Settings,
    ) -> None:
        self.rules, self.channels, self.settings = rules, channels, settings
        self.consents, self.addresses, self.alerts, self.retention = consents, addresses, alerts, retention
        # The platform's own settings (the alert settings service is `settings`).
        self.configured = configured

    @operation("alert_rules.list")
    async def rules_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "alert-rules")
        result = await self.rules.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "alert-rules", request.url.path))

    @operation("alert_rules.read")
    async def rules_read(self, request: Request) -> JSONResponse:
        rule = await self.rules.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return _tagged(rule, request)

    @operation("alert_rules.create")
    async def rules_create(self, request: Request) -> JSONResponse:
        rule = await self.rules.create(
            request.state.principal,
            request_scope(request, environment=True),
            AlertRuleDraft.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return _tagged(rule, request, 201)

    @operation("alert_rules.update")
    async def rules_update(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        rule = await self.rules.update(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            AlertRuleDraft.model_validate_json(await request.body()),
            revision,
            context=request.state.audit_context,
        )
        return _tagged(rule, request)

    @operation("alert_rules.delete")
    async def rules_delete(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        revoked = await self.rules.delete(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            revision,
            context=request.state.audit_context,
        )
        return JSONResponse(revoked.model_dump(mode="json"))

    @operation("alert_rules.preview")
    async def rules_preview(self, request: Request) -> JSONResponse:
        # Health readings are this process's own; a process without background loops reports none degraded.
        health = getattr(request.app.state, "loop_health", None) or LoopHealth()
        preview = await self.rules.preview(
            request.state.principal,
            request_scope(request, environment=True),
            AlertRuleDraft.model_validate_json(await request.body()),
            health=health,
            context=request.state.audit_context,
        )
        return JSONResponse(preview.model_dump(mode="json"))

    @operation("alert_presets.list")
    async def presets_list(self, request: Request) -> JSONResponse:
        presets = await self.rules.presets(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse([preset.model_dump(mode="json") for preset in presets])

    @operation("alert_channels.list")
    async def channels_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "alert-channels")
        result = await self.channels.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "alert-channels", request.url.path))

    @operation("alert_channels.read")
    async def channels_read(self, request: Request) -> JSONResponse:
        channel = await self.channels.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return _tagged(channel, request)

    @operation("alert_channels.create")
    async def channels_create(self, request: Request) -> JSONResponse:
        channel = await self.channels.create(
            request.state.principal,
            request_scope(request, environment=True),
            AlertChannelDraft.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return _tagged(channel, request, 201)

    @operation("alert_channels.update")
    async def channels_update(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        channel = await self.channels.update(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            AlertChannelDraft.model_validate_json(await request.body()),
            revision,
            context=request.state.audit_context,
        )
        return _tagged(channel, request)

    @operation("alert_channels.delete")
    async def channels_delete(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        revoked = await self.channels.delete(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            revision,
            context=request.state.audit_context,
        )
        return JSONResponse(revoked.model_dump(mode="json"))

    @operation("alert_channels.test")
    async def channels_test(self, request: Request) -> JSONResponse:
        queued = await self.channels.test(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(queued.model_dump(mode="json"), status_code=202)

    @operation("alert_settings.read")
    async def settings_read(self, request: Request) -> JSONResponse:
        settings = await self.settings.read(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return _settings(settings, request)

    @operation("alert_settings.update")
    async def settings_update(self, request: Request) -> JSONResponse:
        revision = _revision(request)
        settings = await self.settings.update(
            request.state.principal,
            request_scope(request, environment=True),
            AlertSettingsUpdate.model_validate_json(await request.body()),
            revision,
            context=request.state.audit_context,
        )
        return _settings(settings, request)

    @operation("alert_recipient_consents.list")
    async def consents_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "alert-recipient-consents")
        result = await self.consents.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "alert-recipient-consents", request.url.path))

    @operation("alert_recipient_consents.create")
    async def consents_create(self, request: Request) -> JSONResponse:
        consent = await self.consents.create(
            request.state.principal,
            request_scope(request, environment=True),
            AlertRecipientConsentDraft.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(consent.model_dump(mode="json"), status_code=201)

    @operation("alert_recipient_consents.revoke")
    async def consents_revoke(self, request: Request) -> JSONResponse:
        consent = await self.consents.revoke(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(consent.model_dump(mode="json"))

    @operation("alert_addresses.read_mine")
    async def addresses_read_mine(self, request: Request) -> JSONResponse:
        # The person is the token's, never one named by the path or the body.
        address = await self.addresses.read_mine(
            request.state.principal, request_scope(request).tenant_id, context=request.state.audit_context
        )
        return JSONResponse(address.model_dump(mode="json"))

    @operation("alert_addresses.save_mine")
    async def addresses_save_mine(self, request: Request) -> JSONResponse:
        address = await self.addresses.save_mine(
            request.state.principal,
            request_scope(request).tenant_id,
            MyAlertAddressUpdate.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(address.model_dump(mode="json"))

    @operation("alerts.list")
    async def alerts_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        query = parse_query(request.query_params, AlertQuery)
        position = run_position(decode_cursor_v2(query.cursor, scope, query.cursor_collection("alerts")))
        result = await self.alerts.list(
            request.state.principal,
            scope,
            query,
            None if position is None or position.at is None else (position.at, position.id),
            context=request.state.audit_context,
        )
        return JSONResponse(result)

    @operation("alerts.read")
    async def alerts_read(self, request: Request) -> JSONResponse:
        alert = await self.alerts.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(alert.model_dump(mode="json"))

    @operation("alerts.acknowledge")
    async def alerts_acknowledge(self, request: Request) -> JSONResponse:
        alert = await self.alerts.acknowledge(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(alert.model_dump(mode="json"))

    @operation("alert_events.list")
    async def alert_events_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        query = parse_query(request.query_params, AlertEventQuery)
        position = run_position(decode_cursor_v2(query.cursor, scope, query.cursor_collection("alert-events")))
        result = await self.alerts.events(
            request.state.principal,
            scope,
            query,
            None if position is None or position.at is None else (position.at, position.id),
            context=request.state.audit_context,
        )
        return JSONResponse(result)

    @operation("operations_retention.read")
    async def retention_read(self, request: Request) -> JSONResponse:
        periods = await self.retention.read(
            request.state.principal,
            request_scope(request, environment=True),
            context=request.state.audit_context,
            log_retention_days=self.configured.execution_log_retention_days,
        )
        return JSONResponse(periods.model_dump(mode="json"))
