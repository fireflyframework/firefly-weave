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

"""AI setup owns bounded, scoped transactions and delegates immutable writes."""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, AgenticConnectionAdapter, task_capability
from firefly_weave.contracts.catalog import PublishedVersion
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.ai_facts import builtin_ai_definitions
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction

REFERENCE = "weave-agentic.generate@1.0.0"


def module():
    assert importlib.util.find_spec("firefly_weave.operations.ai_setup"), "AI setup service is unavailable"
    return importlib.import_module("firefly_weave.operations.ai_setup")


def release(**changes):
    return WorkerRelease(
        **dict(
            id=uuid4(),
            image_digest="sha256:" + "a" * 64,
            capabilities=[task_capability()],
            credential_capabilities=[REFERENCE],
        )
        | changes
    )


def test_publication_identity_is_derived_from_immutable_builtins():
    connector, action = builtin_ai_definitions()
    assert connector.reference == "weave-agentic-provider@1.0.0"
    assert connector.definition_digest == "7dfc18419ba042e7dff2a15198373c13da3e1a5e83a78976c422993f57a937e7"
    assert action.definition_digest == "cd1ab7add02d438b419cb206fdb7164c027fe5c5a5b4b3029af2402ce60ebbf0"
    key = module().publication_key(connector)
    assert key == "weave-ai-Connector-" + connector.definition_digest and len(key) == 83
    assert module().publication_key(action) == "weave-ai-Action-" + action.definition_digest
    tx = Transaction(None, Scope(tenant_id=uuid4(), project_id=uuid4()))
    assert Idempotency(tx, uuid4(), "publish:Connector", key, {}).values["key"] == key


@pytest.fixture
def setup(monkeypatch):
    mod = module()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    project = scope.model_copy(update={"environment_id": None})
    actor = Principal(
        id=uuid4(),
        kind="human",
        grants=(Grant(role="developer", scope=project), Grant(role="tenant_admin", scope=scope)),
    )
    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="provider",
        connector_version_id=uuid4(),
        connector="weave-agentic-provider@1.0.0",
        connector_digest=AGENTIC_DESCRIPTOR.manifest.digest,
        adapter="weave-agentic-provider",
        config={"provider": "openai-chat", "endpoint": "https://models.example/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "private-handle"},
        allowed_destinations=("https://models.example",),
    )
    state = SimpleNamespace(
        scope=scope,
        project=project,
        actor=actor,
        current=actor,
        revision=revision,
        context=AuditContext(),
        versions={},
        retired=set(),
        grants=set(),
        audits=[],
        calls=[],
        opened=[],
        releases=[release()],
        project_count=1,
        project_bytes=1,
        release_count=None,
        release_bytes=1,
        grant_count=None,
        grant_bytes=1,
        failure=None,
        active=False,
        tx=None,
        ready_calls=[],
    )

    class Session:
        info = {}

        def get_transaction(self):
            return state.transaction if state.active else None

        async def scalar(self, statement, params=None):
            sql = str(statement)
            if "current_setting" in sql:
                return str(scope.tenant_id)
            assert "connection_revisions" in sql
            assert params["tenant"] == scope.tenant_id and params["project"] == scope.project_id
            assert params["environment"] == scope.environment_id
            state.calls.append("revision")
            return state.revision.model_dump(mode="json", by_alias=True) if params["id"] == state.revision.id else None

        async def execute(self, statement, params):
            sql = str(statement)
            assert state.active and params["tenant"] == scope.tenant_id and params["project"] == scope.project_id
            assert not any(word in sql for word in ("INSERT", "UPDATE", "DELETE"))
            if "worker_releases" in sql:
                if "environment_id" not in sql:
                    assert "count(*)" in sql and "octet_length(payload::text)" in sql
                    state.calls.append("project-size")
                    return SimpleNamespace(one=lambda: (state.project_count, state.project_bytes))
                assert params["environment"] == scope.environment_id
                rows = [{"id": r.id, "payload": r.model_dump(mode="json", by_alias=True)} for r in state.releases]
                count, size, name = state.release_count, state.release_bytes, "releases"
            elif "definition_versions" in sql:
                assert "environment_id" not in sql and state.tx.scope == project
                rows = [
                    dict(
                        kind=v.kind,
                        name=v.name,
                        version=v.version,
                        definition_digest=v.definition_digest,
                        retired=v.id in state.retired,
                    )
                    for v in state.versions.values()
                ]
                count, size, name = None, 1, "published"
            else:
                assert "worker_connection_grants" in sql and "NOT revoked" in sql
                assert params["environment"] == scope.environment_id
                rows = [dict(release_id=r, connection_id=c, capability=k) for r, c, k in state.grants]
                count, size, name = state.grant_count, state.grant_bytes, "grants"
            state.calls.append(name + ("-size" if "count(*)" in sql else "-rows"))
            return SimpleNamespace(
                one=lambda: (len(rows) if count is None else count, size), mappings=lambda: iter(rows)
            )

    session = Session()
    state.session = session

    @asynccontextmanager
    async def opened(given, mutation=True):
        assert mutation and given in (scope, project) and not state.active
        state.opened.append(given)
        saved = deepcopy((state.versions, state.grants, state.audits))
        state.active = True
        state.transaction = SimpleNamespace(is_active=True)
        session.info = {"weave_admission": (state.transaction, scope.tenant_id, scope.project_id)}
        state.tx = Transaction(session, given)
        try:
            yield state.tx
        except BaseException:
            state.versions, state.grants, state.audits = saved
            raise
        finally:
            state.active = False
            state.transaction.is_active = False

    async def current(given, identifier):
        assert state.active and given is session and identifier == state.actor.id
        return state.current

    async def audit(given, principal, action, resource, **kwargs):
        assert state.active and given is session and principal is state.current
        state.audits.append((action, resource, kwargs))

    monkeypatch.setattr(mod, "load_principal", current)
    monkeypatch.setattr(mod, "audit", audit)
    definitions = object.__new__(DefinitionService)
    definitions.authorization = AuthorizationService()
    definitions.uow = SimpleNamespace(open=opened)
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    definitions.registry = registry

    async def publish(actor, given, kind, source, format, key, *, context, tx):
        async with definitions.transaction(given, tx):
            assert tx is state.tx and tx.scope == project and context is state.context and actor is state.actor
            assert format == "json"
            item = next(row for row in builtin_ai_definitions() if row.kind == kind)
            assert source == json.dumps(item.document, sort_keys=True, separators=(",", ":"))
            assert key == "weave-ai-" + kind + "-" + item.definition_digest
            if kind == "Action" and state.failure:
                raise state.failure
            if kind not in state.versions:
                name, version = item.reference.split("@")
                state.versions[kind] = PublishedVersion(
                    id=uuid4(),
                    kind=kind,
                    name=name,
                    version=version,
                    digest="a" * 64,
                    definition_digest=item.definition_digest,
                )
            return state.versions[kind]

    definitions.publish = AsyncMock(side_effect=publish)
    provider = SimpleNamespace(resolve=lambda handle: pytest.fail("Setup resolved a secret"))
    secrets = ScopedSecrets({"test": provider}, (SecretGrant(scope, "private-handle", "test", "value"),))
    connections = ConnectionService(definitions.uow, definitions, registry, secrets)

    async def contract(actor, given, identifier, *, capability, context, tx, resource=None):
        assert given == scope and tx is state.tx and identifier == state.revision.connector_version_id
        state.ready_calls.append(identifier)
        return {
            "definition_digest": AGENTIC_DESCRIPTOR.manifest.digest,
            "document": AGENTIC_DESCRIPTOR.manifest.value,
            "artifact": {"executable": {"dependencies": []}},
        }

    definitions.connector_contract = AsyncMock(side_effect=contract)

    async def grant(actor, given, request, *, context, tx):
        async with definitions.transaction(given, tx):
            assert tx is state.tx and given == scope and actor is state.actor and context is state.context
            if state.failure and state.grants:
                raise state.failure
            state.grants.add((request.release_id, request.connection_revision_id, request.capability))
            state.audits.append(("worker.connection.grant", request.release_id, {}))

    workers = SimpleNamespace(grant_connection=AsyncMock(side_effect=grant))
    state.service = mod.AISetupService(definitions, connections, workers)
    return state


async def publish(s):
    return await s.service.publish(s.actor, s.scope, context=s.context)


async def grant(s, identifier=None):
    request = importlib.import_module("firefly_weave.contracts.ai").AISetupGrantRequest(
        connection_revision_id=identifier or s.revision.id
    )
    return await s.service.grant(s.actor, s.scope, request, context=s.context)


async def test_publication_reuses_versions_across_requests_and_principals(setup):
    first = await publish(setup)
    second = await publish(setup)
    setup.actor = setup.current = setup.actor.model_copy(update={"id": uuid4()})
    third = await publish(setup)
    assert first == second == third and len(first.actions) == 1
    assert len(setup.versions) == 2 and setup.opened == [setup.project] * 3
    assert [row[0] for row in setup.audits] == ["ai.setup.publish"] * 3
    assert all(row[2]["scope"] == setup.project for row in setup.audits)
    assert setup.calls.index("project-size") < setup.calls.index("published-rows")


@pytest.mark.parametrize("kind", ["Connector", "Action"])
@pytest.mark.parametrize("mutation,code", [("retired", "WV-AI-CATALOG-RETIRED"), ("digest", "WV-VERSION-CONFLICT")])
async def test_replays_reject_retired_or_conflicting_catalog_before_any_publication(setup, kind, mutation, code):
    await publish(setup)
    row = setup.versions[kind]
    if mutation == "retired":
        setup.retired.add(row.id)
    else:
        setup.versions[kind] = row.model_copy(update={"definition_digest": "b" * 64})
    setup.service.definitions.publish.reset_mock()
    with pytest.raises(CatalogError) as error:
        await publish(setup)
    assert error.value.status == 409 and error.value.code == code
    setup.service.definitions.publish.assert_not_awaited()
    assert len(setup.audits) == 1


async def test_wrong_installed_manifest_aborts_before_first_publish(setup):
    registry = setup.service.definitions.registry
    registry._descriptors["weave-agentic-provider"] = SimpleNamespace(manifest=FrozenDocument.from_value({"bad": True}))
    with pytest.raises(CatalogError):
        await publish(setup)
    assert not setup.versions and not setup.audits
    setup.service.definitions.publish.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("second publication"), asyncio.CancelledError()])
async def test_second_publication_failure_rolls_back_versions_and_audit(setup, failure):
    setup.failure = failure
    with pytest.raises(type(failure)):
        await publish(setup)
    assert not setup.versions and not setup.audits and not setup.active
    assert setup.service.definitions.publish.await_count == 2


@pytest.mark.parametrize("snapshot", ["actor", "current"])
@pytest.mark.parametrize("operation", ["publish", "grant"])
async def test_setup_requires_both_current_and_token_authority(setup, snapshot, operation):
    setattr(setup, snapshot, getattr(setup, snapshot).model_copy(update={"grants": ()}))
    with pytest.raises(AccessDenied):
        await (publish(setup) if operation == "publish" else grant(setup))
    assert not setup.versions and not setup.grants and not setup.audits and not setup.calls


async def test_environment_only_developer_cannot_publish_project_definitions(setup):
    setup.actor = setup.current = setup.actor.model_copy(
        update={"grants": (Grant(role="developer", scope=setup.scope),)}
    )
    with pytest.raises(AccessDenied):
        await publish(setup)
    assert not setup.versions


@pytest.mark.parametrize("count,size", [(1001, 1), (1, 8 * 1024 * 1024 + 1)])
async def test_project_wide_publication_bound_precedes_payload_loading_and_mutations(setup, count, size):
    setup.project_count, setup.project_bytes = count, size
    with pytest.raises(CatalogError) as error:
        await publish(setup)
    assert error.value.status == 429
    assert setup.calls == ["project-size"] and not setup.versions and not setup.audits


async def test_keyless_grant_skips_releases_grants_and_secret_resolution_even_when_empty(setup):
    from firefly_weave import private_origins

    setup.releases = []
    setup.revision = setup.revision.model_copy(
        update={
            "config": {"provider": "openai-chat", "endpoint": "http://ollama:11434/v1", "secretSlot": "apiKey"},
            "secret_refs": {"apiKey": "no-credential"},
        }
    )
    origins = private_origins.PrivateOrigins(
        platform=private_origins.PLATFORM,
        entries=(
            private_origins.PrivateOrigin(
                origin="http://ollama:11434", purpose="model", credentials="none", networks=("10.246.27.0/24",)
            ),
        ),
    )
    with private_origins.installed(origins):
        result = await grant(setup)
    assert result.model_dump() == {"granted": False, "not_needed": True}
    assert setup.calls == ["revision"] and setup.ready_calls == [setup.revision.connector_version_id]
    assert not setup.grants and setup.audits[0][2]["details"] == {"not_needed": True}


async def test_grant_covers_every_release_beyond_first_page_and_reenables_revoked_pairs(setup):
    setup.releases = [release() for _ in range(101)]
    expected = {(r.id, setup.revision.id, REFERENCE) for r in setup.releases}
    setup.grants = {next(iter(expected))}
    result = await grant(setup)
    assert result.model_dump() == {"granted": True, "not_needed": False}
    assert setup.grants == expected and setup.service.workers.grant_connection.await_count == 100
    setup.service.workers.grant_connection.reset_mock()
    assert await grant(setup) == result
    setup.service.workers.grant_connection.assert_not_awaited()
    setup.grants.remove(next(iter(expected)))
    assert await grant(setup) == result and setup.grants == expected
    assert setup.service.workers.grant_connection.await_count == 1
    assert setup.opened == [setup.scope] * 3


@pytest.mark.parametrize("mutation", ["custom", "version", "digest", "undeclared", "absent"])
def test_credential_pairs_require_actual_exact_recognized_contract_and_credential_declaration(mutation):
    good = release()
    cap = task_capability().model_dump(by_alias=True)
    if mutation == "custom":
        cap["taskType"] = "custom.generate"
    if mutation == "version":
        cap["taskVersion"] = "2.0.0"
    if mutation == "digest":
        cap["timeoutSeconds"] = 599
    ref = cap["taskType"] + "@" + cap["taskVersion"]
    other = release(capabilities=[cap], credential_capabilities=[] if mutation == "undeclared" else [ref])
    if mutation == "absent":
        cap["taskType"] = "custom.generate"
        other = release(capabilities=[cap], credential_capabilities=[])
    assert module().credential_pairs([good, other]) == ((good.id, REFERENCE),)


@pytest.mark.parametrize("empty", ["no_release", "no_credentials", "mismatch"])
async def test_credentialed_connection_without_relevant_pair_is_actionable_422(setup, empty):
    setup.releases = [] if empty == "no_release" else [release(credential_capabilities=[])]
    if empty == "mismatch":
        cap = task_capability().model_copy(update={"timeout_seconds": 599})
        setup.releases = [release(capabilities=[cap])]
    with pytest.raises(CatalogError) as error:
        await grant(setup)
    assert (error.value.status, error.value.code) == (422, "WV-AI-WORKER-MISSING")
    assert not setup.grants and not setup.audits


@pytest.mark.parametrize("group", ["release", "grant"])
@pytest.mark.parametrize("count,size", [(1001, 1), (1, 8 * 1024 * 1024 + 1)])
async def test_grant_bounds_reject_before_any_mutation(setup, group, count, size):
    setattr(setup, group + "_count", count)
    setattr(setup, group + "_bytes", size)
    with pytest.raises(CatalogError) as error:
        await grant(setup)
    assert error.value.status == 429 and not setup.grants and not setup.audits
    assert setup.calls[-1] == ("releases-size" if group == "release" else "grants-size")


@pytest.mark.parametrize("failure", [RuntimeError("second grant"), asyncio.CancelledError()])
async def test_grant_failure_rolls_back_all_pairs_and_audits(setup, failure):
    setup.releases = [release(), release()]
    setup.failure = failure
    with pytest.raises(type(failure)):
        await grant(setup)
    assert not setup.grants and not setup.audits and not setup.active


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter": "other"},
        {"connector": "other@1.0.0"},
        {"connector_digest": "b" * 64},
        {"config": {"provider": "invalid"}},
        {"secret_refs": {"apiKey": "missing"}},
    ],
)
async def test_wrong_or_no_longer_ready_revision_rejects_without_grants(setup, changes):
    setup.revision = setup.revision.model_copy(update=changes)
    with pytest.raises(CatalogError) as error:
        await grant(setup)
    assert error.value.status == 422 and not setup.grants and not setup.audits


async def test_foreign_revision_is_opaque_404(setup):
    with pytest.raises(CatalogError) as error:
        await grant(setup, uuid4())
    assert error.value.status == 404 and not setup.grants and not setup.audits


def test_setup_contracts_forbid_client_catalog_and_grant_overrides():
    module()
    contracts = importlib.import_module("firefly_weave.contracts.ai")
    for extra in ("release_id", "capability", "source", "idempotency_key"):
        with pytest.raises(ValidationError):
            contracts.AISetupGrantRequest.model_validate({"connection_revision_id": str(uuid4()), extra: "x"})
    with pytest.raises(ValidationError):
        contracts.AISetupGrantRequest(connection_revision_id="bad")
    version = PublishedVersion(
        id=uuid4(), kind="Action", name="test", version="1.0.0", digest="a" * 64, definition_digest="b" * 64
    )
    for actions in ([], [version] * 3):
        with pytest.raises(ValidationError):
            contracts.AISetupPublishResult(connector=version, actions=actions)


@pytest.mark.parametrize("operation", ["publish", "grant"])
@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_successful_retry_never_overrides_revoked_authority(setup, operation, snapshot):
    call = publish if operation == "publish" else grant
    await call(setup)
    prior = deepcopy((setup.versions, setup.grants, setup.audits))
    setattr(setup, snapshot, getattr(setup, snapshot).model_copy(update={"grants": ()}))
    with pytest.raises(AccessDenied):
        await call(setup)
    assert (setup.versions, setup.grants, setup.audits) == prior


async def test_release_and_grant_payloads_share_one_budget_before_writes(setup):
    setup.release_bytes = setup.grant_bytes = 4 * 1024 * 1024 + 1
    with pytest.raises(CatalogError) as error:
        await grant(setup)
    assert error.value.status == 429 and not setup.grants and not setup.audits
    assert setup.calls[-1] == "grants-size"


async def test_credential_pairs_never_expand_a_release_to_custom_tasks(setup):
    custom = task_capability().model_copy(update={"task_type": "custom.generate"})
    setup.releases = [
        release(capabilities=[task_capability(), custom], credential_capabilities=[REFERENCE, "custom.generate@1.0.0"])
    ]
    await grant(setup)
    assert setup.grants == {(setup.releases[0].id, setup.revision.id, REFERENCE)}


@pytest.mark.parametrize("operation", ["publish", "grant"])
async def test_setup_requires_project_and_grants_require_environment(setup, operation):
    setup.scope = Scope(
        tenant_id=setup.scope.tenant_id, project_id=None if operation == "publish" else setup.scope.project_id
    )
    with pytest.raises(AccessDenied):
        await (publish(setup) if operation == "publish" else grant(setup))
    assert setup.opened == []
