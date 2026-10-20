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

"""Studio documents: the canvas and the test data a platform draft keeps beside its workflow.

Each save adds a revision and keeps the latest two (so a conflict can be compared). The server derives whether a
test-data document holds data recorded from runs, and from which environments; both stay set for every later
revision until the document is deleted, and reading or writing such a document needs ``run.read`` in each of those
environments. Values are classified against their schemas with the platform's rules before anything is stored, and
problems name document pointers, never values.
"""

import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from pyfly.container.stereotypes import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.test_data import SCHEMA, SECRET, check_test_data
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.studio_documents import (
    STUDIO_DOCUMENT_MAX_BYTES,
    StudioDocument,
    StudioDocumentKind,
    StudioDocumentSave,
    StudioDocumentTooLarge,
    StudioTestData,
    parse_test_data,
)
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService, check_revision, project_scope
from firefly_weave.persistence.idempotency import lock

_RUN_SOURCES = {"run", "test-call"}


def holds_nul(document: object) -> bool:
    """Whether any string or key of the document holds U+0000 (checked without recursion)."""
    stack: list[object] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if "\x00" in node:
                return True
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if "\x00" in key:
                    return True
                stack.append(value)
    return False


def compact_bytes(document: object) -> bytes:
    """A document as Studio sends it and as its size is measured: compact UTF-8, characters unescaped."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def run_data(document: JsonObject | StudioTestData) -> tuple[bool, list[UUID]]:
    """Whether a test-data document holds data recorded from runs, and the environments of those runs.

    Read from the typed entries only (the test input, pins, signals, human answers, AI turns and called-workflow
    mocks) and their provenance fields, never from inside the values they hold: a step may be called ``data`` or
    ``output``, and a value may hold a field named ``runEnvironmentId``. A document that doesn't parse raises: run
    data is never guessed.
    """
    data = document if isinstance(document, StudioTestData) else parse_test_data(compact_bytes(document))
    entries: list[Any] = [data.input]
    for section in (data.pins, data.signals, data.humans, data.ai_turns):
        entries.extend((section or {}).values())
    for callee in (data.workflows or {}).values():
        entries.extend(callee.mocks.values())
    found = False
    environments: set[UUID] = set()
    for entry in entries:
        run_id = getattr(entry, "run_id", None)
        environment = getattr(entry, "run_environment_id", None)
        if run_id is not None or getattr(entry, "source", None) in _RUN_SOURCES:
            found = True
        if environment is not None:
            environments.add(UUID(str(environment)))
    return found, sorted(environments, key=str)


@service
class StudioDocumentService:
    def __init__(self, definitions: DefinitionService, authorization: AuthorizationService) -> None:
        self.definitions = definitions
        self.authorization = authorization

    def _capabilities(self, kind: StudioDocumentKind, *, write: bool) -> tuple[str, ...]:
        if kind == "canvas":
            return ("definition.write",) if write else ("catalog.read",)
        return ("simulate", "definition.write") if write else ("simulate",)

    def _require(
        self,
        actor: Principal,
        scope: Scope,
        kind: StudioDocumentKind,
        environments: list[UUID],
        *,
        write: bool,
        context: AuditContext,
    ) -> None:
        for capability in self._capabilities(kind, write=write):
            self.authorization.require(actor, scope, capability, context=context)
        # Run data is readable only by people who could read those runs.
        for environment in environments:
            self.authorization.require(
                actor,
                Scope(tenant_id=scope.tenant_id, project_id=scope.project_id, environment_id=environment),
                "run.read",
                context=context,
            )

    async def _latest(self, repository: DefinitionRepository, draft: UUID, kind: str) -> dict[str, Any] | None:
        rows = await repository.rows(
            "SELECT * FROM studio_documents WHERE tenant_id=:tenant AND project_id=:project AND draft_id=:draft "
            "AND kind=:kind ORDER BY revision DESC LIMIT 1",
            draft=draft,
            kind=kind,
        )
        return rows[0] if rows else None

    @staticmethod
    def _view(row: dict[str, Any]) -> StudioDocument:
        # The row also holds the tenant and project, which the envelope doesn't show.
        return StudioDocument(
            draft_id=row["draft_id"],
            kind=row["kind"],
            revision=row["revision"],
            draft_revision=row["draft_revision"],
            document=row["document"],
            contains_run_data=row["contains_run_data"],
            source_environment_ids=list(row["source_environment_ids"]),
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )

    async def read(
        self, actor: Principal, scope: Scope, draft: UUID, kind: StudioDocumentKind, *, context: AuditContext
    ) -> StudioDocument:
        scope = project_scope(scope)
        self._require(actor, scope, kind, [], write=False, context=context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            row = await self._latest(DefinitionRepository(tx), draft, kind)
            if row is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Studio document not found")
            principal = await load_principal(tx.session, actor.id)
            self._require(principal, scope, kind, list(row["source_environment_ids"]), write=False, context=context)
            return self._view(row)

    def _document(self, kind: StudioDocumentKind, document: JsonObject) -> StudioTestData | None:
        """Checks a document's characters, size and format; a test-data document comes back parsed, once."""
        if holds_nul(document):
            # PostgreSQL's jsonb can't store U+0000: refused in words, not as a storage failure.
            raise CatalogError(422, "WV-STUDIO-DOCUMENT-SCHEMA", "Studio documents can't hold the NUL character")
        # Measured as sent, never as re-serialized here: escaped characters or spaces would inflate it.
        raw = compact_bytes(document)
        if len(raw) > STUDIO_DOCUMENT_MAX_BYTES[kind]:
            raise CatalogError(413, "WV-STUDIO-DOCUMENT-SIZE", "Studio document is too large")
        if kind != "test-data":
            return None
        try:
            return parse_test_data(raw)
        except StudioDocumentTooLarge:
            raise CatalogError(413, "WV-STUDIO-DOCUMENT-SIZE", "Studio document is too large") from None
        except ValidationError:
            raise CatalogError(422, "WV-STUDIO-DOCUMENT-SCHEMA", "Test data does not match its format") from None

    async def _classify(
        self, tx: Any, repository: DefinitionRepository, draft_document: JsonObject, data: StudioTestData
    ) -> None:
        catalog = await repository.snapshot(await self.definitions.compiler_capabilities(tx))
        secret: list[str] = []
        unclassified: list[str] = []
        for path, code in check_test_data(draft_document, data, catalog):
            if code == SECRET:
                secret.append(path)
            # A value that only fails its output schema stays savable: it shows as out of date in Studio.
            elif code is not None and code != SCHEMA:
                unclassified.append(path)
        if secret:
            raise CatalogError(
                422, "WV-STUDIO-DOCUMENT-SECRET", "Test data holds secret values", result={"paths": secret}
            )
        if unclassified:
            raise CatalogError(
                422,
                "WV-STUDIO-DOCUMENT-UNCLASSIFIED",
                "Test data holds values Weave can't classify",
                result={"paths": unclassified},
            )

    async def save(
        self,
        actor: Principal,
        scope: Scope,
        draft: UUID,
        kind: StudioDocumentKind,
        body: StudioDocumentSave,
        expected_revision: int | None,
        *,
        context: AuditContext,
    ) -> StudioDocument:
        scope = project_scope(scope)
        document = body.document
        # Parsed once (refused when it doesn't parse: run data is never guessed), and read from that parse.
        parsed = self._document(kind, document)
        found, environments = run_data(parsed) if parsed is not None else (False, [])
        self._require(actor, scope, kind, environments, write=True, context=context)
        async with self.definitions.transaction(scope, None) as tx:
            await lock(tx, f"draft:{scope.tenant_id}:{scope.project_id}:{draft}")
            repository = DefinitionRepository(tx)
            if await repository.rows(
                "SELECT id FROM draft_retirements WHERE tenant_id=:tenant AND project_id=:project AND id=:id", id=draft
            ):
                raise CatalogError(409, "WV-DRAFT-RETIRED", "Retired drafts keep no studio documents")
            named = await repository.rows(
                "SELECT revision FROM draft_revisions WHERE tenant_id=:tenant AND project_id=:project AND id=:id "
                "AND revision=:revision",
                id=draft,
                revision=body.draft_revision,
            )
            if not named:
                raise CatalogError(404, "WV-NOT-FOUND", "Draft revision not found")
            # Values are checked against the draft as it is now, whichever revision the client names.
            drafts = await repository.rows(
                "SELECT document FROM draft_revisions WHERE tenant_id=:tenant AND project_id=:project AND id=:id "
                "ORDER BY revision DESC LIMIT 1",
                id=draft,
            )
            current = await self._latest(repository, draft, kind)
            check_revision(current["revision"] if current else None, expected_revision)
            stored = list(current["source_environment_ids"]) if current else []
            merged = sorted({*stored, *environments}, key=str)
            principal = await load_principal(tx.session, actor.id)
            self._require(principal, scope, kind, merged, write=True, context=context)
            if parsed is not None:
                await self._classify(tx, repository, drafts[0]["document"], parsed)
            revision = (current["revision"] if current else 0) + 1
            contains = found or bool(current and current["contains_run_data"])
            await repository.execute(
                "INSERT INTO studio_documents(tenant_id,project_id,draft_id,kind,revision,draft_revision,document,"
                "contains_run_data,source_environment_ids,updated_by) VALUES(:tenant,:project,:draft,:kind,:revision,"
                ":draft_revision,cast(:document AS jsonb),:contains,cast(:environments AS uuid[]),:principal)",
                draft=draft,
                kind=kind,
                revision=revision,
                draft_revision=body.draft_revision,
                document=json.dumps(document),
                contains=contains,
                environments=[str(item) for item in merged],
                principal=actor.id,
            )
            await repository.execute(
                "DELETE FROM studio_documents WHERE tenant_id=:tenant AND project_id=:project AND draft_id=:draft "
                "AND kind=:kind AND revision<:revision-1",
                draft=draft,
                kind=kind,
                revision=revision,
            )
            await audit(
                tx.session,
                actor,
                "studio_document.save",
                str(draft),
                scope=scope,
                capability="definition.write",
                context=context,
                details={"kind": kind, "revision": revision, "contains_run_data": contains},
            )
            row = await self._latest(repository, draft, kind)
            assert row is not None
            return self._view(row)

    async def delete(
        self,
        actor: Principal,
        scope: Scope,
        draft: UUID,
        kind: StudioDocumentKind,
        expected_revision: int | None,
        *,
        context: AuditContext,
    ) -> None:
        scope = project_scope(scope)
        self._require(actor, scope, kind, [], write=True, context=context)
        async with self.definitions.transaction(scope, None) as tx:
            await lock(tx, f"draft:{scope.tenant_id}:{scope.project_id}:{draft}")
            repository = DefinitionRepository(tx)
            current = await self._latest(repository, draft, kind)
            if current is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Studio document not found")
            if expected_revision is None:
                raise CatalogError(412, "WV-ETAG", "Revision precondition failed")
            check_revision(current["revision"], expected_revision)
            principal = await load_principal(tx.session, actor.id)
            self._require(principal, scope, kind, list(current["source_environment_ids"]), write=True, context=context)
            await repository.execute(
                "DELETE FROM studio_documents WHERE tenant_id=:tenant AND project_id=:project AND draft_id=:draft "
                "AND kind=:kind",
                draft=draft,
                kind=kind,
            )
            await audit(
                tx.session,
                actor,
                "studio_document.delete",
                str(draft),
                scope=scope,
                capability="definition.write",
                context=context,
                details={"kind": kind, "revision": current["revision"]},
            )
