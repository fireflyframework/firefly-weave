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

"""Real PostgreSQL actor-bound decisions and durable pause/expiry arbitration."""

import asyncio

import pytest
from sqlalchemy import text

from firefly_weave.access.models import Grant
from firefly_weave.contracts.access import Scope

pytestmark = pytest.mark.integration


@pytest.fixture
async def human_identity(authenticated_client, access_db, provisioned):
    import json
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from firefly_weave.access.authentication import AuthenticationService, VerifierSet
    from firefly_weave.access.models import VerifiedIdentity
    from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig

    access = access_db[2]
    admin, scopes = provisioned
    identifier = await access.create_principal(admin, "human")
    identity = VerifiedIdentity(
        provider_id="human-test",
        issuer="https://human.test.invalid",
        subject="reviewer",
        client_id="human",
        actor_kind="human",
        claims={},
    )
    await access.link_identity(admin, identifier, identity)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "human-test", "alg": "RS256"}

    async def fetch():
        return {"keys": [public]}

    verifier = OIDCVerifier(
        ProviderConfig(
            provider_id="human-test",
            issuer=identity.issuer,
            jwks_uri="https://human.test.invalid/keys",
            audience="api",
            clients={"human": "human"},
        ),
        fetch=fetch,
    )
    authentication = authenticated_client[0]._transport.app.state.pyfly.context.get_bean(AuthenticationService)
    authentication.verifiers = VerifierSet((*authentication.verifiers.items, verifier))
    token = jwt.encode(
        {
            "iss": identity.issuer,
            "sub": "reviewer",
            "aud": "api",
            "azp": "human",
            "typ": "Bearer",
            "exp": int(time.time()) + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": "human-test"},
    )
    return identifier, scopes[0], {"Authorization": "Bearer " + token}


@pytest.fixture
async def headers(human_identity):
    return human_identity[2]


@pytest.fixture
async def author(client, human_identity, access_db, provisioned):
    identifier, scope, _ = human_identity
    for role in ("viewer", "developer", "deployer"):
        await access_db[2].grant(
            provisioned[0],
            identifier,
            Grant(
                role=role,
                scope=Scope(tenant_id=scope.tenant_id, project_id=scope.project_id) if role == "developer" else scope,
            ),
        )
    return client, await access_db[2].load_principal(identifier), scope


SOURCE = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: native-approval, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  timeoutSeconds: 600
  steps:
    - id: review
      kind: humanTask
      assignment: reviewers
      title: {literal: Review expense}
      context: {literal: {amount: 10}}
      formSchema:
        type: object
        properties: {note: {type: string}}
        required: [note]
        additionalProperties: false
      expirySeconds: 300
  output: {ref: /steps/review/output}
"""


@pytest.fixture
async def human_run(author, access_db, provisioned, headers, project_url, env_url, request):
    for role in ("operator", "task_participant", "task_manager"):
        await access_db[2].grant(provisioned[0], author[1].id, Grant(role=role, scope=author[2]))
    candidates = {"principal_ids": [str(author[1].id)]}
    if getattr(request, "param", False):
        group = await author[0].post(
            env_url + "/human-groups",
            headers={**headers, "Idempotency-Key": "group"},
            json={"name": "reviewers", "member_ids": [str(author[1].id)]},
        )
        assert group.status_code == 200, group.text
        candidates = {"group_ids": [group.json()["id"]]}
    binding = await author[0].post(
        env_url + "/human-assignments",
        headers={**headers, "Idempotency-Key": "reviewers"},
        json={"name": "reviewers", **candidates},
    )
    assert binding.status_code == 200, binding.text
    response = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "human-flow"},
        json={"format": "yaml", "source": SOURCE},
    )
    assert response.status_code == 201, response.text
    published = response.json()
    response = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "human-activation"},
        json={
            "version_id": published["id"],
            "artifact_digest": published["digest"],
            "scope": author[2].model_dump(mode="json"),
            "assignment_binding_ids": {"reviewers": binding.json()["binding_id"]},
        },
    )
    assert response.status_code == 201, response.text
    response = await author[0].post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "human-run"},
        json={"activation_id": response.json()["id"], "input": {}},
    )
    assert response.status_code == 201, response.text
    tasks = await author[0].get(env_url + "/human-tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text
    assert len(tasks.json()["items"]) == 1
    return response.json()["id"], tasks.json()["items"][0]


async def mutate(client, headers, env_url, task, command, payload, key):
    return await client.post(
        f"{env_url}/human-tasks/{task['id']}/{command}", headers={**headers, "Idempotency-Key": key}, json=payload
    )


@pytest.mark.parametrize("historical", [False, True])
async def test_claim_complete_retry_and_conflicting_payload(
    client, headers, env_url, human_run, access_db, author, historical
):
    run, task = human_run
    if historical:
        async with access_db[1]() as session, session.begin():
            await session.execute(text("DELETE FROM step_facts WHERE run_id=:id"), {"id": run})
    claimed = await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")
    assert claimed.status_code == 200, claimed.text
    payload = {"expected_revision": 2, "decision": "reject", "data": {"note": "No"}}
    completed = await mutate(client, headers, env_url, task, "complete", payload, "complete")
    assert completed.status_code == 200, completed.text
    retried = await mutate(client, headers, env_url, task, "complete", payload, "complete")
    assert retried.status_code == 200 and retried.json() == completed.json(), retried.text
    changed = await mutate(client, headers, env_url, task, "complete", {**payload, "decision": "approve"}, "complete")
    assert changed.status_code == 409, changed.text
    view = (await client.get(f"{env_url}/runs/{run}", headers=headers)).json()
    assert view["state"]["status"] == "succeeded" and view["state"]["output"]["decision"] == "reject"
    detail = await client.get(f"{env_url}/human-tasks/{task['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert [entry["action"] for entry in detail.json()["history"]] == ["created", "claim", "complete"]
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM human_task_decisions")) == 1
        fact = (
            (
                await session.execute(
                    text("SELECT kind,status,scheduled_at,started_at,ended_at,attempts FROM step_facts")
                )
            )
            .mappings()
            .one()
        )
        assert fact["kind"] == "humanTask" and fact["status"] == "succeeded"
        assert fact["ended_at"] is not None and fact["attempts"] == 0
        if historical:
            assert fact["scheduled_at"] is None and fact["started_at"] is None
        else:
            assert fact["scheduled_at"] == fact["started_at"] <= fact["ended_at"]
        assert await session.scalar(text("SELECT count(*) FROM task_intents")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_deadlines WHERE NOT consumed")) == 0
        from operations_support import assert_usage

        await assert_usage(session, {"tenant": author[2].tenant_id, "project": author[2].project_id})


async def test_pause_allows_fact_then_resume_continues(client, headers, env_url, human_run):
    run, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    paused = await client.post(
        f"{env_url}/runs/{run}/pause",
        headers={**headers, "Idempotency-Key": "pause"},
        json={"expected_revision": 0, "reason": "Review"},
    )
    assert paused.status_code == 200, paused.text
    completed = await mutate(
        client,
        headers,
        env_url,
        task,
        "complete",
        {"expected_revision": 2, "decision": "approve", "data": {"note": "yes"}},
        "complete",
    )
    assert completed.status_code == 200, completed.text
    view = (await client.get(f"{env_url}/runs/{run}", headers=headers)).json()
    assert view["state"]["manual_paused"] and view["state"]["status"] == "waiting"
    resumed = await client.post(
        f"{env_url}/runs/{run}/resume",
        headers={**headers, "Idempotency-Key": "resume"},
        json={"expected_revision": 1, "reason": "Continue"},
    )
    assert resumed.status_code == 200 and resumed.json()["state"]["status"] == "succeeded", resumed.text


async def test_concurrent_claim_accepts_once(client, headers, env_url, human_run):
    _, task = human_run
    responses = await asyncio.gather(
        *(mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, f"claim-{i}") for i in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]


async def test_expiry_and_cancellation_close_task(client, headers, env_url, human_run, access_db, services, author):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    run, task = human_run
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='review'")
        )
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 1
    read = await client.get(f"{env_url}/human-tasks/{task['id']}", headers=headers)
    assert read.status_code == 200 and read.json()["status"] == "expired", read.text
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 409
    assert (await client.get(f"{env_url}/runs/{run}", headers=headers)).json()["state"]["status"] == "timed_out"


async def test_application_cannot_claim_even_with_participant_grant(
    client, authenticated_client, headers, env_url, human_run, access_db, provisioned, author
):
    _, task = human_run
    async with access_db[1]() as session:
        app_id = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], app_id, Grant(role="task_participant", scope=author[2]))
    application_headers = {"Authorization": "Bearer " + authenticated_client[1][0]}
    response = await mutate(client, application_headers, env_url, task, "claim", {"expected_revision": 1}, "claim")
    assert response.status_code == 403, response.text


async def test_revoked_capability_blocks_new_decision_and_terminal_retry(
    client, headers, env_url, human_run, access_db, author
):
    _, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    payload = {"expected_revision": 2, "decision": "approve", "data": {"note": "ok"}}
    complete = await mutate(client, headers, env_url, task, "complete", payload, "complete")
    assert complete.status_code == 200, complete.text
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='task_participant'"), {"id": author[1].id}
        )
    retry = await mutate(client, headers, env_url, task, "complete", payload, "complete")
    assert retry.status_code == 403, retry.text


@pytest.mark.parametrize("human_run", [True], indirect=True)
async def test_group_revocation_after_claim_blocks_completion(client, headers, env_url, human_run):
    _, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    response = await client.post(
        env_url + "/human-groups",
        headers={**headers, "Idempotency-Key": "remove-member"},
        json={"name": "reviewers", "member_ids": [], "expected_revision": 1},
    )
    assert response.status_code == 200, response.text
    rejected = await mutate(
        client,
        headers,
        env_url,
        task,
        "complete",
        {"expected_revision": 2, "decision": "approve", "data": {"note": "ok"}},
        "complete",
    )
    assert rejected.status_code == 403, rejected.text


async def test_cancel_closes_task_and_pause_fences_claim(client, headers, env_url, human_run):
    run, task = human_run
    paused = await client.post(
        f"{env_url}/runs/{run}/pause",
        headers={**headers, "Idempotency-Key": "pause"},
        json={"expected_revision": 0, "reason": "Hold"},
    )
    assert paused.status_code == 200, paused.text
    rejected = await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")
    assert rejected.status_code == 409, rejected.text
    cancelled = await client.post(f"{env_url}/runs/{run}/cancel", headers=headers, json={"reason": "Cancel"})
    assert cancelled.status_code == 200, cancelled.text
    read = await client.get(f"{env_url}/human-tasks/{task['id']}", headers=headers)
    assert read.status_code == 200 and read.json()["status"] == "cancelled", read.text


async def test_exact_scope_rls_hides_tasks_and_audit(human_run, access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    async with UnitOfWork(access_db[0]).open(provisioned[1][1]) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM human_tasks")) == 0
        assert await tx.session.scalar(text("SELECT count(*) FROM human_task_audit")) == 0
        assert await tx.session.scalar(text("SELECT count(*) FROM human_assignment_bindings")) == 0


async def test_concurrent_complete_records_one_immutable_decision(client, headers, env_url, human_run, access_db):
    _, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    payload = {"expected_revision": 2, "decision": "approve", "data": {"note": "ok"}}
    responses = await asyncio.gather(
        *(mutate(client, headers, env_url, task, "complete", payload, f"complete-{i}") for i in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM human_task_decisions")) == 1


async def test_completion_races_cancellation_under_run_lock(client, headers, env_url, human_run, access_db):
    run, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    complete, cancel = await asyncio.gather(
        mutate(
            client,
            headers,
            env_url,
            task,
            "complete",
            {"expected_revision": 2, "decision": "approve", "data": {"note": "ok"}},
            "complete",
        ),
        client.post(f"{env_url}/runs/{run}/cancel", headers=headers, json={"reason": "Cancel"}),
    )
    assert sorted((complete.status_code, cancel.status_code)) == [200, 409], [complete.text, cancel.text]
    state = (await client.get(f"{env_url}/runs/{run}", headers=headers)).json()["state"]
    assert state["status"] in {"succeeded", "cancelled"}
    async with access_db[1]() as session:
        count = await session.scalar(text("SELECT count(*) FROM human_task_decisions"))
    assert count == (1 if state["status"] == "succeeded" else 0)


async def test_due_expiry_wins_concurrent_completion(client, headers, env_url, human_run, access_db, services, author):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    run, task = human_run
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp() WHERE node_id='review'"))

    async def expire():
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            return await services(access_db[0]).resolve(DeadlineService).tick(tx, 100)

    complete, _ = await asyncio.gather(
        mutate(
            client,
            headers,
            env_url,
            task,
            "complete",
            {"expected_revision": 2, "decision": "approve", "data": {"note": "ok"}},
            "complete",
        ),
        expire(),
    )
    assert complete.status_code == 409, complete.text
    assert (await client.get(f"{env_url}/runs/{run}", headers=headers)).json()["state"]["status"] == "timed_out"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM human_task_decisions")) == 0


async def test_run_keys_filter_distinct_executions_and_bind_cursor(human_run, author, headers, env_url):
    env_url = "/api/v1" + env_url
    client = author[0]
    existing = await client.get(env_url + "/runs/" + human_run[0], headers=headers)
    activation = existing.json()["activation"]["id"]
    responses = await asyncio.gather(
        *[
            client.post(
                env_url + "/runs",
                headers={**headers, "Idempotency-Key": "keyed-" + str(i)},
                json={
                    "activation_id": activation,
                    "input": {},
                    "business_key": "expense-42",
                    "correlation_key": "thread-7",
                },
            )
            for i in range(2)
        ]
    )
    assert all(r.status_code == 201 for r in responses)
    assert responses[0].json()["id"] != responses[1].json()["id"]
    filters = {
        "business_key": "expense-42",
        "correlation_key": "thread-7",
        "status": "waiting",
        "include_archived": "false",
        "limit": 1,
    }
    first = await client.get(env_url + "/runs", headers=headers, params=filters)
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 1
    cursor = first.json()["next_cursor"]
    assert cursor
    second = await client.get(env_url + "/runs", headers=headers, params={**filters, "cursor": cursor})
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]
    mismatch = await client.get(
        env_url + "/runs", headers=headers, params={**filters, "business_key": "other", "cursor": cursor}
    )
    assert mismatch.status_code == 422
    invalid = await client.get(env_url + "/runs", headers=headers, params={"status": "invalid"})
    assert invalid.status_code == 422


async def test_archive_requires_terminal_and_purge_removes_task_data(
    client, headers, env_url, human_run, access_db, author, provisioned
):
    run, task = human_run
    url = f"{env_url}/runs/{run}"
    payload = {"expected_revision": 0, "reason": "Case retention complete"}
    archive_headers = {**headers, "Idempotency-Key": "archive"}
    pending = await client.post(url + "/archive", headers=archive_headers, json=payload)
    assert pending.status_code == 409
    assert (await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim")).status_code == 200
    result = await mutate(
        client,
        headers,
        env_url,
        task,
        "complete",
        {"expected_revision": 2, "decision": "approve", "data": {"note": "Accepted"}},
        "complete",
    )
    assert result.status_code == 200, result.text
    assert (await client.post(url + "/archive", headers=archive_headers, json=payload)).status_code == 200
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="execution_manager", scope=author[2]))
    result = await client.post(
        url + "/purge",
        headers={**headers, "Idempotency-Key": "purge"},
        json={**payload, "expected_revision": 1, "confirm_run_id": run},
    )
    assert result.status_code == 200, result.text
    assert (await client.get(f"{env_url}/human-tasks/{task['id']}", headers=headers)).status_code == 404
    async with access_db[1]() as session:
        for table in ("human_tasks", "human_task_decisions", "human_task_audit"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        cached = (
            (
                await session.execute(
                    text("SELECT response FROM mutation_idempotency WHERE operation LIKE 'human-task:%'")
                )
            )
            .scalars()
            .all()
        )
        assert cached and all(item.get("_weave_purged") is True for item in cached)


async def test_task_restricted_grant_lists_and_decides_only_selected_task(
    human_run, author, headers, env_url, access_db, provisioned
):
    client, principal, scope = author
    _, task = human_run
    run = await client.get(env_url + "/runs/" + human_run[0], headers=headers)
    other_run = await client.post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "other-run"},
        json={"activation_id": run.json()["activation"]["id"], "input": {}},
    )
    assert other_run.status_code == 201
    tasks = await client.get(env_url + "/human-tasks", headers=headers)
    other = next(value for value in tasks.json()["items"] if value["id"] != task["id"])
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role IN ('task_participant','task_manager')"),
            {"id": principal.id},
        )
    await access_db[2].grant(
        provisioned[0], principal.id, Grant(role="task_participant", scope=scope, resources=(task["id"],))
    )
    listed = await client.get(env_url + "/human-tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [value["id"] for value in listed.json()["items"]] == [task["id"]]
    assert (await client.get(env_url + "/human-tasks/" + task["id"], headers=headers)).status_code == 200
    assert (await client.get(env_url + "/human-tasks/" + other["id"], headers=headers)).status_code == 403
    denied = await mutate(client, headers, env_url, other, "claim", {"expected_revision": 1}, "other-claim")
    assert denied.status_code == 403, denied.text
    claimed = await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, "allowed-claim")
    assert claimed.status_code == 200, claimed.text
    released = await mutate(client, headers, env_url, task, "release", {"expected_revision": 2}, "allowed-release")
    assert released.status_code == 200, released.text
    claimed = await mutate(client, headers, env_url, task, "claim", {"expected_revision": 3}, "allowed-reclaim")
    assert claimed.status_code == 200, claimed.text
    completed = await mutate(
        client,
        headers,
        env_url,
        task,
        "complete",
        {"expected_revision": 4, "decision": "approve", "data": {"note": "restricted"}},
        "allowed-complete",
    )
    assert completed.status_code == 200, completed.text
    assert (await client.get(env_url + "/human-tasks/" + task["id"], headers=headers)).status_code == 200
    assignment = await client.post(
        env_url + "/human-assignments",
        headers={**headers, "Idempotency-Key": "denied-binding"},
        json={"name": "broader-reviewers", "principal_ids": [str(principal.id)]},
    )
    assert assignment.status_code == 403
    await access_db[2].grant(provisioned[0], principal.id, Grant(role="task_manager", scope=scope))
    assignment = await client.post(
        env_url + "/human-assignments",
        headers={**headers, "Idempotency-Key": "restricted-candidate-binding"},
        json={"name": "broader-reviewers", "principal_ids": [str(principal.id)]},
    )
    assert assignment.status_code == 403, assignment.text
    group = await client.post(
        env_url + "/human-groups",
        headers={**headers, "Idempotency-Key": "restricted-candidate-group"},
        json={"name": "broader-reviewers", "member_ids": [str(principal.id)]},
    )
    assert group.status_code == 403, group.text
