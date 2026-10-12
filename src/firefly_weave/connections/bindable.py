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

"""Bounded, currently authorized connection choices without configuration or credentials."""

import hashlib
import json
from uuid import UUID

from pyfly.container import service
from sqlalchemy import ARRAY, String, Uuid, bindparam, text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.connections.diagnostics import is_agentic_revision
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.service import ConnectionService
from firefly_weave.connectors.egress import origin
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.bindable_connections import BindableConnection, BindableConnectionQuery
from firefly_weave.contracts.public import Page
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.private_origins import canonical_origin

SCAN_LIMIT = 100
SCAN_PAGES = 10
PAGE_BYTES = 8 * 1024 * 1024


def endpoint_origin(endpoint: str) -> str | None:
    try:
        scheme, host, port = origin(endpoint)
        rendered_host = f"[{host}]" if ":" in host else host
        return canonical_origin(f"{scheme}://{rendered_host}:{port}", "model")
    except ValueError:
        return None


def _bind_resources(principal: Principal, scope: Scope) -> set[UUID] | None:
    grants = [
        grant
        for grant in principal.grants
        if "connection.bind" in ROLE_CAPABILITIES[grant.role] and contains(grant.scope, scope)
    ]
    if any(not grant.resources for grant in grants):
        return None
    identifiers = set()
    for grant in grants:
        for resource in grant.resources:
            if len(resource) != 36:
                continue
            try:
                identifier = UUID(resource)
            except ValueError:
                continue
            # Authorization compares exact resource strings, not equivalent UUID spellings.
            if str(identifier) == resource:
                identifiers.add(identifier)
    return identifiers


def _allowed_revision_ids(actor: Principal, current: Principal, scope: Scope) -> list[UUID] | None:
    token_ids, current_ids = _bind_resources(actor, scope), _bind_resources(current, scope)
    identifiers = current_ids if token_ids is None else token_ids if current_ids is None else token_ids & current_ids
    if identifiers is None:
        return None
    # Compact JSON: two brackets and, per canonical UUID, 36 ASCII bytes, quotes and a separator.
    if identifiers and 39 * len(identifiers) + 1 > PAGE_BYTES:
        raise CatalogError(429, "WV-PAGE-LIMIT", "Selected grant parameters exceed the logical byte limit")
    return sorted(identifiers)


async def _candidate_ids(
    tx: Transaction, *, connector: str | None, after: UUID | None, allowed_ids: list[UUID] | None
) -> list[tuple[UUID, int]]:
    statement = text(
        "SELECT id, octet_length(payload::text) AS logical_bytes FROM connection_revisions "
        "WHERE tenant_id=:tenant AND project_id=:project AND environment_id=:environment "
        "AND (:after IS NULL OR id>:after) "
        "AND (:connector IS NULL OR payload->>'connector'=:connector) "
        + ("AND id=ANY(:allowed_ids) " if allowed_ids is not None else "")
        + "ORDER BY id LIMIT :limit"
    ).bindparams(bindparam("after", type_=Uuid), bindparam("connector", type_=String))
    parameters: dict[str, object] = {
        "tenant": tx.scope.tenant_id,
        "project": tx.scope.project_id,
        "environment": tx.scope.environment_id,
        "after": after,
        "connector": connector,
        "limit": SCAN_LIMIT + 1,
    }
    if allowed_ids is not None:
        statement = statement.bindparams(bindparam("allowed_ids", type_=ARRAY(Uuid)))
        parameters["allowed_ids"] = allowed_ids
    rows = await tx.session.execute(statement, parameters)
    return [(row.id, row.logical_bytes) for row in rows.all()]


@service
class BindableConnectionService:
    def __init__(self, connections: ConnectionService) -> None:
        self.connections = connections

    def require_bind_scope(self, actor: Principal, current: Principal, scope: Scope, context: AuditContext) -> None:
        for principal in (actor, current):
            resource = next(
                (
                    grant.resources[0] if grant.resources else None
                    for grant in principal.grants
                    if "connection.bind" in ROLE_CAPABILITIES[grant.role] and contains(grant.scope, scope)
                ),
                None,
            )
            self.connections.definitions.authorization.require(
                principal, scope, "connection.bind", resource=resource, context=context
            )

    async def list(
        self, actor: Principal, scope: Scope, query: BindableConnectionQuery, *, context: AuditContext
    ) -> Page[BindableConnection]:
        if scope.environment_id is None:
            raise AccessDenied()
        binding = json.dumps(
            {"connector": query.connector, "principal_id": str(actor.id)}, sort_keys=True, separators=(",", ":")
        )
        collection = "bindable-connections:" + hashlib.sha256(binding.encode()).hexdigest()
        position = decode_cursor_v2(query.cursor, scope, collection)
        after = None
        if position is not None:
            sort_value, cursor_identifier = position
            after = UUID(cursor_identifier)
            if sort_value is not None or str(after) != cursor_identifier:
                raise ValueError("Invalid scope-bound cursor")
        items: list[BindableConnection] = []
        selected_bytes = 0
        last_authorized = None
        async with self.connections.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.require_bind_scope(actor, current, scope, context)
            allowed_ids = _allowed_revision_ids(actor, current, scope)
            if allowed_ids == []:
                return Page(items=[])
            for page in range(SCAN_PAGES):
                candidates = await _candidate_ids(tx, connector=query.connector, after=after, allowed_ids=allowed_ids)
                selected_bytes += sum(size for _, size in candidates[:SCAN_LIMIT])
                if selected_bytes > PAGE_BYTES:
                    raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
                for index, (identifier, _) in enumerate(candidates[:SCAN_LIMIT]):
                    after = identifier
                    try:
                        self.connections.require_revision(actor, current, scope, identifier, "connection.bind", context)
                    except AccessDenied:
                        continue
                    last_authorized = identifier
                    revision = await ConnectionRepository(tx).revision(identifier)
                    enabled = True
                    try:
                        await self.connections._ready(
                            current, scope, revision, "connection.bind", context, tx, resource=str(identifier)
                        )
                    except CatalogError as error:
                        if error.code not in {"WV-CONNECTION", "WV-LEGACY-UNAVAILABLE", "WV-NOT-FOUND"}:
                            raise
                        enabled = False
                    ai = is_agentic_revision(revision)
                    safe_origin = endpoint_origin(str(revision.config.get("endpoint", ""))) if ai else None
                    items.append(
                        BindableConnection(
                            connection_id=revision.id,
                            revision_id=revision.id,
                            name=revision.name,
                            label=revision.name,
                            connector=revision.connector,
                            enabled=enabled and (not ai or safe_origin is not None),
                            provider=str(revision.config["provider"]) if ai else None,
                            endpoint_origin=safe_origin,
                        )
                    )
                    if len(items) == query.limit:
                        cursor = (
                            encode_cursor_v2(scope, collection, None, str(last_authorized))
                            if index + 1 < len(candidates)
                            else None
                        )
                        return Page(items=items, next_cursor=cursor)
                if len(candidates) <= SCAN_LIMIT:
                    return Page(items=items)
                if page == SCAN_PAGES - 1:
                    if last_authorized is None:
                        raise CatalogError(
                            429, "WV-PAGE-LIMIT", "Selected page cannot safely continue within the scan limit"
                        )
                    return Page(
                        items=items, next_cursor=encode_cursor_v2(scope, collection, None, str(last_authorized))
                    )
        return Page(items=items)
