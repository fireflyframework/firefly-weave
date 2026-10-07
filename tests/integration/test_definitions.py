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

"""Catalog acceptance against the guarded nonowner PostgreSQL runtime."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.access import Scope

pytestmark = pytest.mark.integration


def decision_publication():
    return {
        "format": "json",
        "source": json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "DecisionTable",
                "metadata": {"name": "decision-policy", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "string"},
                    "hitPolicy": "first",
                    "rules": [{"id": "approve", "when": {"literal": True}, "output": {"literal": "approved"}}],
                },
            }
        ),
    }


async def test_decision_table_publication_lock_retirement_and_tenant_isolation(
    author, headers, other_headers, project_url, access_db, provisioned
):
    url = project_url + "/decision-tables"
    body = decision_publication()
    response = await author[0].post(url, headers={**headers, "Idempotency-Key": "decision"}, json=body)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    assert response.json()["kind"] == "DecisionTable"
    replay = await author[0].post(url, headers={**headers, "Idempotency-Key": "decision"}, json=body)
    assert replay.json() == response.json()
    listed = await author[0].get(url, headers=headers)
    assert listed.status_code == 200 and listed.json()["items"][0]["id"] == identity
    exported = await author[0].get(url + "/" + identity + "/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.json()["artifact"]["executable"]["irVersion"] == "weave/ir-v1alpha3"
    denied = await author[0].get(url + "/" + identity, headers=other_headers)
    assert denied.status_code == 403
    catalog = await author[0].get(project_url + "/catalog", headers=headers)
    assert any(d["document"]["kind"] == "DecisionTable" for d in catalog.json()["definitions"])
    changed = {**body, "source": body["source"].replace('"approved"', '"rejected"')}
    conflict = await author[0].post(url, headers={**headers, "Idempotency-Key": "decision-conflict"}, json=changed)
    assert conflict.status_code == 409
    await access_db[2].grant(
        provisioned[0],
        author[1].id,
        Grant(role="deployer", scope=Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)),
    )
    retired = await author[0].post(
        url + "/" + identity + "/retire", headers={**headers, "Idempotency-Key": "retire-decision"}, json={}
    )
    assert retired.status_code == 200, retired.text


async def test_pure_decision_evaluation_requires_compile_access_and_never_persists_input(
    author, headers, other_headers, project_url, access_db, provisioned, caplog
):
    body = {**decision_publication(), "input": {"private": "never-persist-this"}}
    url = project_url + "/compiler/evaluate-decision"
    denied = await author[0].post(url, headers=other_headers, json=body)
    assert denied.status_code == 403
    async with access_db[1]() as observer:
        viewer = await observer.scalar(text("SELECT principal_id FROM identity_links WHERE subject='1'"))
    await access_db[2].grant(
        provisioned[0],
        viewer,
        Grant(role="viewer", scope=Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)),
    )
    assert (await author[0].get(project_url + "/catalog", headers=other_headers)).status_code == 200
    denied = await author[0].post(url, headers=other_headers, json=body)
    assert denied.status_code == 403
    result = await author[0].post(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json() == {"output": "approved", "matched_rule_ids": ["approve"], "used_default": False}
    invalid = {**body, "source": body["source"].replace('"literal": true', '"literal": false')}
    rejected = await author[0].post(url, headers=headers, json=invalid)
    assert rejected.status_code == 422 and rejected.json()["code"] == "WV-DECISION-NO_MATCH"
    assert "never-persist-this" not in rejected.text
    assert "never-persist-this" not in caplog.text
    async with access_db[1]() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM definition_versions")) == 0
        assert await observer.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await observer.scalar(text("SELECT count(*) FROM task_intents")) == 0


async def test_published_decision_runs_without_workers_and_keeps_table_pin_after_retirement(
    author, headers, project_url, env_url, access_db, provisioned
):
    table = await author[0].post(
        project_url + "/decision-tables",
        headers={**headers, "Idempotency-Key": "decision"},
        json=decision_publication(),
    )
    assert table.status_code == 201, table.text
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "decision-flow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "string"},
            "steps": [
                {"id": "choose", "kind": "decisionTable", "uses": "decision-policy@1.0.0", "with": {"ref": "/input"}}
            ],
            "output": {"ref": "/steps/choose/output"},
        },
    }
    flow = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "decision-flow"},
        json={"source": json.dumps(document), "format": "json"},
    )
    assert flow.status_code == 201, flow.text
    activation = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "decision-activation"},
        json={
            "version_id": flow.json()["id"],
            "artifact_digest": flow.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert activation.status_code == 201, activation.text
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    await access_db[2].grant(
        provisioned[0],
        author[1].id,
        Grant(role="deployer", scope=Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)),
    )
    retired = await author[0].post(
        project_url + "/decision-tables/" + table.json()["id"] + "/retire",
        headers={**headers, "Idempotency-Key": "retire-decision"},
        json={},
    )
    assert retired.status_code == 200, retired.text
    run = await author[0].post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "decision-run"},
        json={"activation_id": activation.json()["id"], "input": {}},
    )
    assert run.status_code == 201, run.text
    assert run.json()["state"]["status"] == "succeeded"
    assert run.json()["state"]["output"] == "approved"
    async with access_db[1]() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM task_intents")) == 0
        transition = await observer.scalar(text("SELECT transition FROM run_events"))
        decision = next(step["decision"] for step in transition["steps"] if step["node_id"] == "choose")
        assert decision == {"matched_rule_ids": ["approve"], "used_default": False}


@pytest.fixture
async def author(client, authenticated_client, access_db, provisioned):
    sessions, _, access, _ = access_db
    admin, scopes = provisioned
    async with sessions.begin() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    project = Scope(tenant_id=scopes[0].tenant_id, project_id=scopes[0].project_id)
    await access.grant(admin, identifier, Grant(role="developer", scope=project))
    await access.grant(admin, identifier, Grant(role="deployer", scope=scopes[0]))
    return client, await access.load_principal(identifier), scopes[0]


@pytest.fixture
def publication_request():
    return {
        "format": "yaml",
        "source": """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: pure, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  steps:
    - {id: value, kind: transform, value: {literal: 7}}
  output: {ref: /steps/value/output}
""",
    }


async def publish(author, headers, project_url, publication_request, key="publish-1"):
    return await author[0].post(
        project_url + "/workflows", headers={**headers, "Idempotency-Key": key}, json=publication_request
    )


async def test_publication_key_cannot_change_content(author, headers, project_url, publication_request):
    first = await publish(author, headers, project_url, publication_request)
    second = await publish(
        author,
        headers,
        project_url,
        {**publication_request, "source": publication_request["source"] + "\n# changed request"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 409


async def retry_capacity(operation):
    """Retry only declared overload; preserve each caller's original idempotency key."""
    async with asyncio.timeout(10):
        for attempt in range(8):
            response = await operation()
            if response.status_code != 429:
                return response
            assert response.json()["code"] in {"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"}, response.text
            await asyncio.sleep(0.05 * (attempt + 1))
    pytest.fail("Capacity did not recover within eight bounded attempts")


async def test_concurrent_publication_and_lost_response(author, headers, project_url, publication_request, access_db):
    async def attempt(key):
        return await retry_capacity(lambda: publish(author, headers, project_url, publication_request, key))

    results = await asyncio.gather(*(attempt(f"key-{i}") for i in range(8)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    retry = await attempt("key-0")
    assert retry.status_code == 201 and retry.json() == results[0].json()
    same = await asyncio.gather(*(attempt("shared") for _ in range(5)))
    assert all(r.status_code == 201 and r.json() == results[0].json() for r in same)
    async with access_db[1]() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM definition_versions")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM definition_sources")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM mutation_idempotency")) == 9


async def test_semantic_republish_and_changed_version(author, headers, project_url, publication_request):
    first = await publish(author, headers, project_url, publication_request)
    same = await publish(
        author,
        headers,
        project_url,
        {**publication_request, "source": "# comment\n" + publication_request["source"]},
        "format-change",
    )
    assert first.status_code == same.status_code == 201
    assert first.json() == same.json()
    changed = await publish(
        author,
        headers,
        project_url,
        {**publication_request, "source": publication_request["source"].replace("literal: 7", "literal: 8")},
        "different",
    )
    assert changed.status_code == 409
    export = await author[0].get(project_url + "/workflows/" + first.json()["id"] + "/export", headers=headers)
    assert export.status_code == 200
    assert len(export.json()["sources"]) == 2
    assert export.json()["artifact"]["digest"] == first.json()["digest"]


async def test_compile_parity_literal_routes_and_forged_artifact(author, headers, project_url, publication_request):
    bad = {"format": "yaml", "source": "kind: Unknown"}
    expected = json.loads(compile_source(bad["source"], format="yaml", catalog=CatalogSnapshot.empty()).to_bytes())
    response = await author[0].post(project_url + "/compiler/compile", headers=headers, json=bad)
    assert response.status_code == 200
    assert response.json() == expected
    published = await publish(author, headers, project_url, bad)
    assert published.status_code == 422
    assert published.json()["result"] == expected
    forged = await publish(author, headers, project_url, {**publication_request, "digest": "0" * 64})
    assert forged.status_code == 422
    drafts = await author[0].get(project_url + "/drafts", headers=headers)
    assert drafts.status_code == 200


async def test_draft_etag_and_history(author, headers, project_url):
    url = project_url + "/drafts/" + str(uuid4())
    first = await author[0].put(url, headers=headers, json={"document": {"incomplete": True}})
    assert first.status_code == 201, first.text
    second = await author[0].put(
        url, headers={**headers, "If-Match": first.headers["etag"]}, json={"document": {"incomplete": False}}
    )
    assert second.status_code == 200
    stale = await author[0].put(url, headers={**headers, "If-Match": first.headers["etag"]}, json={"document": {}})
    assert stale.status_code == 412
    missing = await author[0].put(url, headers=headers, json={"document": {}})
    assert missing.status_code == 412
    history = await author[0].get(url + "/export", headers=headers)
    assert len(history.json()["revisions"]) == 2


async def test_activation_retirement_and_environment_pins(author, headers, project_url, env_url, publication_request):
    published = await publish(author, headers, project_url, publication_request)
    assert published.status_code == 201, published.text
    body = {
        "version_id": published.json()["id"],
        "artifact_digest": published.json()["digest"],
        "scope": author[2].model_dump(mode="json"),
        "connection_revision_ids": {},
        "worker_release_ids": {},
    }
    url = env_url + "/activations"
    first = await author[0].post(url, headers={**headers, "Idempotency-Key": "activate"}, json=body)
    assert first.status_code == 201, first.text
    retry = await author[0].post(url, headers={**headers, "Idempotency-Key": "activate"}, json=body)
    assert retry.json() == first.json()
    stale = await author[0].post(url, headers={**headers, "Idempotency-Key": "stale"}, json=body)
    assert stale.status_code == 412
    other = {**body, "scope": {**body["scope"], "environment_id": str(uuid4())}}
    rejected = await author[0].post(url, headers={**headers, "Idempotency-Key": "cross"}, json=other)
    assert rejected.status_code == 422
    unsupported = {**body, "connection_revision_ids": {"x": str(uuid4())}}
    rejected = await author[0].post(
        url, headers={**headers, "Idempotency-Key": "unsupported", "If-Match": first.headers["etag"]}, json=unsupported
    )
    assert rejected.status_code == 422
    retired = await author[0].post(
        project_url + "/workflows/" + published.json()["id"] + "/retire",
        headers={**headers, "Idempotency-Key": "retire"},
        json={},
    )
    # Retirement affects a project-wide version: an environment deployer cannot retire it globally.
    assert retired.status_code == 403
    detail = await author[0].get(url + "/" + first.json()["id"], headers=headers)
    assert detail.status_code == 200


async def test_service_checks_actual_project_scope(services, author, access_db, publication_request):
    from firefly_weave.definitions.service import DefinitionService

    actor, scope = author[1:]
    limited = actor.model_copy(update={"grants": (Grant(role="developer", scope=scope),)})
    service = services(access_db[0]).resolve(DefinitionService)
    with pytest.raises(AccessDenied):
        await service.publish(
            limited, scope, "Workflow", **publication_request, idempotency_key="direct", context=AuditContext()
        )


async def test_scope_list_cursor_and_missing_key(author, headers, other_headers, project_url, publication_request):
    missing = await author[0].post(project_url + "/workflows", headers=headers, json=publication_request)
    assert missing.status_code == 422
    denied = await author[0].post(
        project_url + "/workflows", headers={**other_headers, "Idempotency-Key": "x"}, json=publication_request
    )
    assert denied.status_code == 403
    first = await publish(author, headers, project_url, publication_request)
    assert first.status_code == 201
    for suffix in ("?limit=101", "?cursor=not-a-uuid"):
        result = await author[0].get(project_url + "/workflows" + suffix, headers=headers)
        assert result.status_code == 422
    result = await author[0].get(project_url + "/workflows?limit=1", headers=headers)
    assert len(result.json()["items"]) == 1
    UUID(result.json()["items"][0]["id"])


async def test_retired_version_and_activation_remain_readable(
    author, headers, project_url, env_url, publication_request, access_db, provisioned
):
    _, _, access, _ = access_db
    await access.grant(
        provisioned[0],
        author[1].id,
        Grant(role="deployer", scope=Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)),
    )
    first = await publish(author, headers, project_url, publication_request)
    body = {
        "version_id": first.json()["id"],
        "artifact_digest": first.json()["digest"],
        "scope": author[2].model_dump(mode="json"),
    }
    activation = await author[0].post(env_url + "/activations", headers={**headers, "Idempotency-Key": "a"}, json=body)
    assert activation.status_code == 201
    version_url = project_url + "/workflows/" + first.json()["id"]
    retired = await author[0].post(version_url + "/retire", headers={**headers, "Idempotency-Key": "r"}, json={})
    assert retired.status_code == 200
    retained = await author[0].get(version_url + "/export", headers=headers)
    assert retained.json()["artifact"]["digest"] == first.json()["digest"]
    assert retained.json()["retired"] is True
    rejected = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "b", "If-Match": activation.headers["etag"]},
        json=body,
    )
    assert rejected.status_code == 422
    activation_url = env_url + "/activations/" + activation.json()["id"]
    history = await author[0].get(activation_url, headers=headers)
    assert history.json() == activation.json()
    retired_activation = await author[0].post(
        activation_url + "/retire",
        headers={**headers, "Idempotency-Key": "ra", "If-Match": activation.headers["etag"]},
        json={},
    )
    assert retired_activation.status_code == 200
    assert retired_activation.json()["retired"] is True
    assert retired_activation.json()["revision"] == 2


async def test_replay_requires_current_grants_and_audit_is_transactional(
    services, author, headers, project_url, publication_request, access_db
):
    from firefly_weave.definitions.service import DefinitionService

    first = await publish(author, headers, project_url, publication_request)
    service = services(access_db[0]).resolve(DefinitionService)
    actor = author[1].model_copy(update={"grants": ()})
    with pytest.raises(AccessDenied):
        await service.publish(
            actor, author[2], "Workflow", **publication_request, idempotency_key="publish-1", context=AuditContext()
        )
    async with access_db[1].begin() as session:
        records = (
            (await session.execute(text("SELECT event FROM access_audit WHERE action='definition.publish'")))
            .scalars()
            .all()
        )
        assert len(records) == 1
        record = records[0]
        assert record["principal_id"] == str(author[1].id)
        assert record["correlation"]["request_id"] == first.headers["x-weave-request-id"]
        assert record["scope"]["environment_id"] is None
        assert record["capability"] == "definition.publish"
        assert record["outcome"] == "success"


async def test_capabilities_are_explicit_server_catalog_inputs(services, author, access_db):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService

    connector = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "example", "version": "1.0.0"},
        "spec": {
            "adapter": "test-adapter",
            "configSchema": {},
            "authSchema": {},
            "actions": {
                "get": {"inputSchema": {}, "outputSchema": {}, "sideEffect": "read_only", "timeoutSeconds": 10}
            },
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 10},
        },
    }
    service = services(access_db[0]).resolve(DefinitionService)
    with pytest.raises(CatalogError) as rejected:
        await service.publish(
            author[1], author[2], "Connector", json.dumps(connector), "json", "connector", context=AuditContext()
        )
    assert rejected.value.status == 422
    configured = services(access_db[0], CatalogSnapshot.from_definitions([], adapters=["test-adapter"])).resolve(
        DefinitionService
    )
    version = await configured.publish(
        author[1], author[2], "Connector", json.dumps(connector), "json", "connector", context=AuditContext()
    )
    assert version.kind == "Connector"
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "get-example", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "example@1.0.0", "action": "get"},
            "inputSchema": {},
            "outputSchema": {},
            "sideEffect": "read_only",
            "timeoutSeconds": 10,
        },
    }
    result = await configured.publish(
        author[1], author[2], "Action", json.dumps(action), "json", "action", context=AuditContext()
    )
    assert result.kind == "Action"


async def test_catalog_tables_force_rls_and_immutable_grants(services, author, access_db, publication_request):
    from sqlalchemy.exc import DBAPIError

    from firefly_weave.definitions.service import DefinitionService

    version = (
        await services(access_db[0])
        .resolve(DefinitionService)
        .publish(author[1], author[2], "Workflow", **publication_request, idempotency_key="db", context=AuditContext())
    )
    async with access_db[0].begin() as session:
        assert await session.scalar(text("SELECT count(*) FROM definition_versions")) == 0
    async with access_db[0].begin() as session:
        await session.execute(text("SELECT set_config('weave.tenant_id',:id,true)"), {"id": str(author[2].tenant_id)})
        assert await session.scalar(text("SELECT count(*) FROM definition_versions")) == 1
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE definition_versions SET name='tampered' WHERE id=:id"), {"id": version.id}
            )
    async with access_db[1].begin() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN "
                    "('definition_versions','definition_sources','definition_retirements','draft_revisions','activation_revisions','mutation_idempotency')"
                )
            )
        ).all()
        assert len(flags) == 6 and all(a and b for a, b in flags)


async def test_retirement_rejects_ignored_payload(
    author, headers, project_url, publication_request, access_db, provisioned
):
    await access_db[2].grant(
        provisioned[0],
        author[1].id,
        Grant(role="deployer", scope=Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)),
    )
    version = await publish(author, headers, project_url, publication_request)
    result = await author[0].post(
        project_url + "/workflows/" + version.json()["id"] + "/retire",
        headers={**headers, "Idempotency-Key": "retire"},
        json={"unexpected": "changed-content"},
    )
    assert result.status_code == 422


async def test_different_content_race_and_scoped_foreign_keys(
    author, headers, project_url, publication_request, access_db, provisioned
):
    from sqlalchemy.exc import DBAPIError

    other_request = {**publication_request, "source": publication_request["source"].replace("literal: 7", "literal: 9")}
    results = await asyncio.gather(
        retry_capacity(lambda: publish(author, headers, project_url, publication_request, "original")),
        retry_capacity(lambda: publish(author, headers, project_url, other_request, "changed")),
    )
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    version = next(r.json() for r in results if r.status_code == 201)
    async with access_db[0].begin() as session:
        await session.execute(text("SELECT set_config('weave.tenant_id',:id,true)"), {"id": str(author[2].tenant_id)})
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO activation_revisions VALUES(:id,:tenant,:project,:environment,'pure',1,:version,'{}')"
                ),
                {
                    "id": uuid4(),
                    "tenant": author[2].tenant_id,
                    "project": author[2].project_id,
                    "environment": provisioned[1][1].environment_id,
                    "version": UUID(version["id"]),
                },
            )


async def test_shared_transaction_cross_service_rollback(services, author, access_db, publication_request):
    from firefly_weave.access.service import audit
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import UnitOfWork

    authoring = services(access_db[0]).resolve(DefinitionService)
    deployment = services(access_db[0]).resolve(DefinitionService)
    actor, environment = author[1:]
    project = Scope(tenant_id=environment.tenant_id, project_id=environment.project_id)
    context = AuditContext()

    async def staged_deployment(tx):
        # A downstream coordinator enlists both catalog use cases and access audit.
        version = await authoring.publish(
            actor,
            project,
            "Workflow",
            **publication_request,
            idempotency_key="shared-publication",
            context=context,
            tx=tx,
        )
        draft = await authoring.save_draft(actor, project, uuid4(), {"stage": "review"}, None, context=context, tx=tx)
        assert (await authoring.read(actor, project, "drafts", draft.id, context=context, tx=tx))["revision"] == 1
        assert len((await authoring.list(actor, project, "Workflow", context=context, tx=tx))["items"]) == 1
        assert (await authoring.compile(actor, project, **publication_request, context=context, tx=tx)).ok
        request = ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=environment)
        await deployment.activate(actor, environment, request, "shared-activation", context=context, tx=tx)
        await audit(
            tx.session,
            actor,
            "coordinator.staged",
            str(version.id),
            scope=environment,
            capability="release.activate",
            context=context,
        )
        # A separately opened connection must not observe any committed participant.
        async with access_db[1].begin() as observer:
            assert await observer.scalar(text("SELECT count(*) FROM definition_versions")) == 0
            assert await observer.scalar(text("SELECT count(*) FROM activation_revisions")) == 0

    with pytest.raises(RuntimeError, match="abort coordinator"):
        async with UnitOfWork(access_db[0]).open(project) as tx:
            await staged_deployment(tx)
            raise RuntimeError("abort coordinator")
    async with access_db[1].begin() as observer:
        for table in (
            "definition_versions",
            "definition_sources",
            "draft_revisions",
            "activation_revisions",
            "mutation_idempotency",
        ):
            assert await observer.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM access_audit WHERE event->'correlation'->>'operation_id'=:id"),
                {"id": str(context.operation_id)},
            )
            == 0
        )
    # The same keys remain usable; the caller's normal exit now commits all participants.
    async with UnitOfWork(access_db[0]).open(project) as tx:
        await staged_deployment(tx)
    async with access_db[1].begin() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM definition_versions")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM activation_revisions")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM mutation_idempotency")) == 2
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM access_audit WHERE event->'correlation'->>'operation_id'=:id"),
                {"id": str(context.operation_id)},
            )
            == 4
        )


@pytest.mark.parametrize("mismatch", ["project", "tenant", "environment", "unbound", "inactive"])
async def test_shared_transaction_rejects_invalid_scope_or_session(
    services, author, access_db, publication_request, provisioned, mismatch
):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import Transaction, UnitOfWork

    actor, scope = author[1:]
    project = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)
    service = services(access_db[0]).resolve(DefinitionService)

    async def attempt(tx):
        with pytest.raises((AccessDenied, CatalogError)):
            await service.publish(
                actor,
                project,
                "Workflow",
                **publication_request,
                idempotency_key="wrong-tx",
                context=AuditContext(),
                tx=tx,
            )

    if mismatch in {"unbound", "inactive"}:
        async with access_db[0]() as session:
            if mismatch == "unbound":
                async with session.begin():
                    await attempt(Transaction(session, project))
            else:
                await attempt(Transaction(session, project))
    else:
        bound = {
            "project": Scope(tenant_id=scope.tenant_id, project_id=uuid4()),
            "tenant": provisioned[1][1],
            "environment": scope,
        }[mismatch]
        async with UnitOfWork(access_db[0]).open(bound) as tx:
            await attempt(tx)
    async with access_db[1].begin() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM definition_versions")) == 0


@pytest.mark.parametrize("character", [" ", "é"])
@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_http_compiler_source_byte_boundary_parity(
    author, headers, project_url, publication_request, character, extra_bytes
):
    base = publication_request["source"] + "#"
    remainder = 1_048_576 + extra_bytes - len(base.encode())
    width = len(character.encode())
    source = base + character * (remainder // width) + " " * (remainder % width)
    assert len(source.encode()) == 1_048_576 + extra_bytes
    expected = json.loads(compile_source(source, format="yaml", catalog=CatalogSnapshot.empty()).to_bytes())
    response = await author[0].post(
        project_url + "/compiler/compile", headers=headers, json={"source": source, "format": "yaml"}
    )
    assert response.status_code == 200
    assert response.json() == expected
    publication = await publish(author, headers, project_url, {"source": source, "format": "yaml"})
    if extra_bytes:
        assert expected["diagnostics"][0]["code"] == "WV-PARSE-SOURCE_LIMIT"
        assert publication.status_code == 422
        assert publication.json()["result"] == expected
    else:
        assert expected["ok"]
        assert publication.status_code == 201, publication.text


async def test_shared_retirement_rollback_and_replay_authorization(
    services, author, access_db, provisioned, publication_request
):
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import UnitOfWork

    project = Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="deployer", scope=project))
    actor = await access_db[2].load_principal(author[1].id)
    service = services(access_db[0]).resolve(DefinitionService)
    version = await service.publish(
        actor, project, "Workflow", **publication_request, idempotency_key="published", context=AuditContext()
    )
    request = ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=author[2])
    activation = await service.activate(actor, author[2], request, "activated", context=AuditContext())
    context = AuditContext()
    with pytest.raises(RuntimeError, match="abort retirement"):
        async with UnitOfWork(access_db[0]).open(project) as tx:
            denied = actor.model_copy(update={"grants": ()})
            with pytest.raises(AccessDenied):
                await service.publish(
                    denied,
                    project,
                    "Workflow",
                    **publication_request,
                    idempotency_key="published",
                    context=context,
                    tx=tx,
                )
            await service.retire_activation(
                actor, author[2], activation.id, "retire-activation", 1, context=context, tx=tx
            )
            await service.retire(actor, project, version.id, "Workflow", "retire-version", context=context, tx=tx)
            assert (await service.read(actor, project, "Workflow", version.id, context=context, tx=tx))["retired"]
            async with access_db[1].begin() as observer:
                assert await observer.scalar(text("SELECT count(*) FROM definition_retirements")) == 0
                assert await observer.scalar(text("SELECT count(*) FROM activation_revisions")) == 1
            raise RuntimeError("abort retirement")
    async with access_db[1].begin() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM definition_retirements")) == 0
        assert await observer.scalar(text("SELECT count(*) FROM activation_revisions")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM mutation_idempotency")) == 2
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM access_audit WHERE event->'correlation'->>'operation_id'=:id"),
                {"id": str(context.operation_id)},
            )
            == 0
        )
