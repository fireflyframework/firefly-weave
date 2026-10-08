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

"""Bounded fixed-snapshot startup inventory over a separate catalog authority."""

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

from pyfly.container import service
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.broker import BrokerTrigger, BrokerTriggerRequest, SourceBinding
from firefly_weave.contracts.compatibility import CompatibilityFinding, CompatibilityReport, FindingCode
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.integration_events import Subscription, SubscriptionRequest
from firefly_weave.contracts.providers import ProviderSource, ProviderSourceRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.compatibility_catalog import classify_requirement
from firefly_weave.operations.execution import execute_pure, inventory_execution
from firefly_weave.operations.telemetry import TelemetryService
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

CATALOG_CLEANUP_SECONDS = 5.0
CATALOG_SCAN_SECONDS = 60.0
INVENTORY_RESCAN_SECONDS = 60.0
INVENTORY_AGE_SECONDS = 5.0


class InventoryAuthorityError(ValueError):
    pass


async def check_inventory_authority(connection: AsyncConnection) -> None:
    await check_schema(connection)
    valid = await connection.scalar(
        text("""
        SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb
          AND pg_has_role(current_user,'weave_scheduler','MEMBER')
          AND NOT pg_has_role(current_user,'weave_app','MEMBER')
          AND NOT pg_has_role(current_user,'weave_catalog_reader','MEMBER')
          AND NOT pg_has_role(current_user,'weave_retention_owner','MEMBER')
          AND NOT EXISTS(SELECT 1 FROM pg_class c WHERE c.relnamespace='public'::regnamespace
            AND c.relkind IN ('r','p') AND c.relname NOT IN ('alembic_version','weave_schema_version')
            AND (pg_has_role(current_user,c.relowner,'MEMBER') OR
              has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR
              has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
        FROM pg_roles WHERE rolname=current_user
    """)
    )
    if valid is not True:
        raise InventoryAuthorityError("Independent catalog authority unavailable")


def classify_inventory_row(
    kind: str, payload: dict[str, Any], facts_valid: object, registry: ConnectorRegistry
) -> FindingCode | None:
    if facts_valid is not True:
        return "inventory_incomplete"
    if kind in {"provider", "broker", "subscription"}:
        try:
            models: dict[
                str,
                tuple[
                    type[ProviderSource] | type[BrokerTrigger] | type[Subscription],
                    type[ProviderSourceRequest] | type[BrokerTriggerRequest] | type[SubscriptionRequest],
                    str,
                ],
            ] = {
                "provider": (ProviderSource, ProviderSourceRequest, "provider-source"),
                "broker": (BrokerTrigger, BrokerTriggerRequest, "kafka-trigger"),
                "subscription": (Subscription, SubscriptionRequest, "outbox-subscription"),
            }
            model, request, source_kind = models[kind]
            source = model.model_validate_json(json.dumps(payload["source"], allow_nan=False))
            facts = payload["binding"]
            binding = SourceBinding.model_validate_json(json.dumps({k: facts[k] for k in SourceBinding.model_fields}))
            if (
                binding.id != source.binding_id
                or binding.source_kind != source_kind
                or binding.source_id != source.id
                or binding.principal_id != source.principal_id
                or binding.connection_revision_id != source.connection_revision_id
                or binding.source_fingerprint
                != canonical_digest(source.model_dump(mode="json", include=set(request.model_fields)))
            ):
                return "inventory_incomplete"
        except (ValueError, TypeError, KeyError):
            return "inventory_incomplete"
        return classify_requirement("provider", payload["source"], registry) if kind == "provider" else None
    if kind == "connection":
        try:
            revision = ConnectionRevision.model_validate_json(json.dumps(payload, allow_nan=False))
            descriptor = registry.descriptor(revision.adapter)
            if descriptor.manifest.digest != revision.connector_digest:
                return "connector_unsupported"
            registry.validate_connection(revision.adapter, revision)
        except (ValueError, TypeError, KeyError, CatalogError):
            return "connector_unsupported"
        return None
    return classify_requirement(kind, payload, registry)


def log_withdrawal(findings: list[CompatibilityFinding], error: BaseException | None) -> None:
    """Name why a rescan withdrew readiness without logging values or exception messages."""
    causes = ",".join(sorted({f"{finding.kind}:{finding.code}" for finding in findings})) or "none"
    cause = "none" if error is None else type(error).__name__
    if isinstance(error, CatalogError):
        cause = f"{cause} {error.code}"
    logging.getLogger(__name__).warning("Compatibility rescan withdrew readiness: findings=%s error=%s", causes, cause)


class FindingAccumulator:
    """Admission sees every finding even when presentation is truncated."""

    def __init__(self) -> None:
        self.items: list[CompatibilityFinding] = []
        self.truncated = self.blocking = self.incomplete = False

    def record(self, finding: CompatibilityFinding) -> None:
        self.incomplete |= finding.code == "inventory_incomplete"
        self.blocking |= finding.code not in {
            "legacy_policy_blocked",
            "operational_capacity_blocked",
            "capacity_absent",
        }
        if len(self.items) < 1000:
            self.items.append(finding)
        else:
            self.truncated = True


@service
class CompatibilityService:
    def __init__(
        self,
        settings: Settings,
        registry: ConnectorRegistry,
        uow: UnitOfWork,
        authorization: AuthorizationService,
        telemetry: TelemetryService,
    ) -> None:
        self.settings, self.registry, self.uow, self.authorization, self.telemetry = (
            settings,
            registry,
            uow,
            authorization,
            telemetry,
        )
        self.report = CompatibilityReport()
        self._scan_lock = asyncio.Lock()
        self._inventory: dict[str, int] = {}
        self._observed_at: float | None = None
        self._freshness_task: asyncio.Task[None] | None = None
        self._opened_at: float | None = None
        self._completed_at: float | None = None
        self._next_scan = math.inf
        self._catalog_engine: AsyncEngine | None = None
        self.on_ready: Callable[[], Awaitable[None]] | None = None
        self.on_restricted: Callable[[], Awaitable[None]] | None = None

    def observe_inventory(self, values: dict[str, int], *, complete: bool) -> None:
        if complete:
            self._inventory = dict(values)
            self._observed_at = time.monotonic()
            self.refresh_inventory_age()

    def refresh_inventory_age(self) -> None:
        if self._observed_at is not None:
            age = max(0.0, time.monotonic() - self._observed_at)
            for operation, count in self._inventory.items():
                self.telemetry.inventory(operation, count, observed_age=age, complete=True)

    async def open(self) -> None:
        if self._freshness_task is None:
            self._opened_at = time.monotonic()
            self._next_scan = self._opened_at + INVENTORY_RESCAN_SECONDS

            async def refresh() -> None:
                while True:
                    await asyncio.sleep(INVENTORY_AGE_SECONDS)
                    self.refresh_inventory_age()
                    if time.monotonic() >= self._next_scan and not self._scan_lock.locked():
                        try:
                            await self.scan()
                        except Exception:
                            logging.getLogger(__name__).error("Compatibility refresh failed")
                        self._next_scan = time.monotonic() + INVENTORY_RESCAN_SECONDS

            self._freshness_task = asyncio.create_task(refresh(), name="weave-inventory-freshness")

    async def close(self) -> None:
        if self._freshness_task is not None:
            self._freshness_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._freshness_task
            self._freshness_task = None
        await self._dispose_catalog()

    async def _dispose_catalog(self) -> None:
        if self._catalog_engine is not None:
            async with asyncio.timeout(CATALOG_CLEANUP_SECONDS):
                await self._catalog_engine.dispose()
            self._catalog_engine = None

    @property
    def completion_budget_seconds(self) -> float:
        # A transition can drain retained owners, initialize effects, then drain a
        # failed initialization. Include both passes without timing out their owner.
        transition = 2 * (
            self.settings.shutdown_timeout_seconds
            + self.settings.database_timeout_seconds
            + self.settings.broker.cleanup_seconds
            + 3 * 5.0
        )
        return (
            INVENTORY_RESCAN_SECONDS
            + 2 * INVENTORY_AGE_SECONDS
            + CATALOG_SCAN_SECONDS
            + 2 * CATALOG_CLEANUP_SECONDS
            + transition
        )

    def healthy(self) -> bool:
        observed = self._completed_at if self._completed_at is not None else self._opened_at
        return (
            self._freshness_task is not None
            and not self._freshness_task.done()
            and observed is not None
            and time.monotonic() - observed <= self.completion_budget_seconds
        )

    @property
    def report(self) -> CompatibilityReport:
        if self.healthy():
            return self._report
        return self._report.model_copy(update={"mode": "restricted", "complete": False})

    @report.setter
    def report(self, value: CompatibilityReport) -> None:
        self._report = value

    @property
    def ready(self) -> bool:
        report = self.report
        return report.mode == "ready" and report.complete

    def retry_after_seconds(self) -> int:
        """Whole seconds until the next periodic rescan can change a restricted verdict."""
        # The refresh loop notices a due rescan on its next age tick.
        wait = min(self._next_scan - time.monotonic(), INVENTORY_RESCAN_SECONDS) + INVENTORY_AGE_SECONDS
        return max(1, math.ceil(wait))

    @asynccontextmanager
    async def _completed_scan(self) -> AsyncIterator[None]:
        try:
            yield
        finally:
            self._completed_at = time.monotonic()

    async def scan(self) -> CompatibilityReport:
        if self._scan_lock.locked():
            from firefly_weave.definitions.models import CatalogError

            raise CatalogError(429, "WV-OPERATION-CAPACITY", "Compatibility scan already in progress")
        async with self._scan_lock, self._completed_scan():
            findings = FindingAccumulator()
            inspected = 0
            complete = traversed = False
            engine = None
            failure: BaseException | None = None
            swallowed: Exception | None = None
            inventory: dict[str, int] = {}

            record = findings.record

            try:
                await self._dispose_catalog()
                if self.settings.scheduler_database_url is None:
                    record(CompatibilityFinding(kind="inventory", code="authority_missing"))
                else:
                    runtime_url = make_url(self.settings.database_url.get_secret_value())
                    catalog_url = make_url(self.settings.scheduler_database_url.get_secret_value())
                    if (runtime_url.drivername, runtime_url.host, runtime_url.port or 5432, runtime_url.database) != (
                        catalog_url.drivername,
                        catalog_url.host,
                        catalog_url.port or 5432,
                        catalog_url.database,
                    ) or runtime_url.query != catalog_url.query:
                        raise InventoryAuthorityError("Catalog endpoint must match runtime endpoint")
                    engine = create_async_engine(
                        self.settings.scheduler_database_url.get_secret_value(),
                        hide_parameters=True,
                        isolation_level="REPEATABLE READ",
                        connect_args={"timeout": 5},
                    )
                    self._catalog_engine = engine
                    async with (
                        inventory_execution(),
                        asyncio.timeout(CATALOG_SCAN_SECONDS),
                        engine.connect() as connection,
                        connection.begin(),
                    ):
                        await connection.execute(text("SET TRANSACTION READ ONLY"))
                        await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
                        await check_inventory_authority(connection)
                        fingerprint = await connection.scalar(text("SELECT weave_compatibility_policy()"))
                        if fingerprint != self.settings.operations.fingerprint:
                            record(CompatibilityFinding(kind="inventory", code="policy_mismatch"))
                        inventory = dict(await connection.scalar(text("SELECT weave_compatibility_counts()")) or {})
                        after_tenant = None
                        while True:
                            tenants: list[UUID] = list(
                                (
                                    await connection.execute(
                                        text("SELECT weave_compatibility_tenants(:after,16)"), {"after": after_tenant}
                                    )
                                ).scalars()
                            )
                            if not tenants:
                                complete = True
                                break
                            for tenant in tenants:
                                after_kind = after_id = None
                                while True:
                                    rows = (
                                        (
                                            await connection.execute(
                                                text("SELECT * FROM weave_compatibility_page(:tenant,:kind,:id,25)"),
                                                {"tenant": tenant, "kind": after_kind, "id": after_id},
                                            )
                                        )
                                        .mappings()
                                        .all()
                                    )
                                    if not rows:
                                        break
                                    for row in rows:
                                        if inspected >= 100000:
                                            raise TimeoutError
                                        inspected += 1
                                        scope = Scope(
                                            tenant_id=tenant,
                                            project_id=row["project_id"],
                                            environment_id=row["environment_id"],
                                        )
                                        if row["payload"] is None:
                                            code: FindingCode | None = "inventory_incomplete"
                                        else:
                                            code = await execute_pure(
                                                partial(
                                                    classify_inventory_row,
                                                    row["kind"],
                                                    row["payload"],
                                                    row["facts_valid"],
                                                    self.registry,
                                                ),
                                                control=True,
                                            )
                                        if code:
                                            record(
                                                CompatibilityFinding(
                                                    kind=row["kind"], code=code, scope=scope, resource_id=row["id"]
                                                )
                                            )
                                    after_kind, after_id = rows[-1]["kind"], rows[-1]["id"]
                            after_tenant = tenants[-1]
                traversed = True
            except asyncio.CancelledError as error:
                failure = error
                record(CompatibilityFinding(kind="inventory", code="inventory_incomplete"))
            except InventoryAuthorityError as error:
                swallowed = error
                record(CompatibilityFinding(kind="inventory", code="authority_missing"))
            except Exception as error:
                swallowed = error
                record(CompatibilityFinding(kind="inventory", code="inventory_incomplete"))
            finally:
                # Readiness must be withdrawn before fallible or stalled cleanup, unless this
                # rescan confirms the ready verdict in force: its effect owners are already
                # initialized and cleanup is bounded, so only a failed cleanup withdraws it.
                was_ready = self.ready
                confirmed = was_ready and traversed and complete and not findings.incomplete and not findings.blocking
                if not confirmed:
                    self.report = CompatibilityReport(
                        checked_at=datetime.now(UTC), findings=findings.items, findings_truncated=findings.truncated
                    )
                try:
                    await self._dispose_catalog()
                except BaseException as error:
                    failure = failure or error
                    record(CompatibilityFinding(kind="inventory", code="inventory_incomplete"))
            complete = complete and not findings.incomplete
            self.observe_inventory(inventory, complete=complete)
            candidate = CompatibilityReport(
                mode="ready" if complete and not findings.blocking else "restricted",
                complete=complete,
                inspected=inspected,
                checked_at=datetime.now(UTC),
                findings=findings.items,
                findings_truncated=findings.truncated,
            )
            confirmed = confirmed and candidate.mode == "ready"
            # A compatible inventory alone does not establish initialized effect owners.
            withdrawn = candidate.model_copy(update={"mode": "restricted"})
            if not confirmed:
                self.report = withdrawn
            if was_ready and candidate.mode == "restricted":
                log_withdrawal(findings.items, swallowed or failure)
            callback = self.on_ready if candidate.mode == "ready" else self.on_restricted
            if callback is not None:
                try:
                    await callback()
                except BaseException:
                    self.report = withdrawn
                    raise
            if failure is not None:
                raise failure
            self.report = candidate
            self.telemetry.record(
                "compatibility", operation="compatibility", status="ok" if candidate.mode == "ready" else "blocked"
            )
        return self.report

    async def scoped(
        self, actor: Principal, scope: Scope, *, context: AuditContext, refresh: bool = False
    ) -> CompatibilityReport:
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(
                current, scope, "compatibility.check" if refresh else "status.read", context=context
            )
        if refresh:
            await self.scan()
        selected = [
            f
            for f in self.report.findings
            if f.scope is None or (f.scope.tenant_id == scope.tenant_id and f.scope.project_id == scope.project_id)
        ]
        return self.report.model_copy(update={"findings": selected, "inspected": 0})
