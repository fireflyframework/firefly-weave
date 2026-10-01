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

"""Real PostgreSQL creator/revision/expiry and transport parity acceptance."""

import pytest
import test_definitions as catalog_tests

pytestmark = pytest.mark.integration
author = catalog_tests.author


async def test_debug_session_persists_exact_steps(author, headers, project_url, worker_runtime_fixture):
    response = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json={
            "artifact": __import__("json").loads(worker_runtime_fixture["artifact"].to_bytes()),
            "mocks": {"node:work": 8},
            "input": 3,
            "now": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["view"]["events"] == []
    url = project_url + "/debug/sessions/" + created["id"]
    stepped = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["revision"] == 2
    assert len(stepped.json()["view"]["events"]) == 1
    stale = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
    assert stale.status_code == 409
    read = await author[0].get(url, headers=headers)
    assert read.json() == stepped.json()


async def created_session(author, headers, project_url, artifact):
    import json

    response = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json={
            "artifact": json.loads(artifact.to_bytes()),
            "mocks": {"node:work": 8},
            "input": 3,
            "now": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return project_url + "/debug/sessions/" + response.json()["id"], response.json()


async def test_creator_only_current_project_grant_and_scope(
    author, headers, project_url, worker_runtime_fixture, access_db, provisioned, other_headers
):
    from sqlalchemy import text

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.access import Scope

    url, created = await created_session(author, headers, project_url, worker_runtime_fixture["artifact"])
    async with access_db[1]() as session:
        other_id = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='1'"))
    scope = Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)
    await access_db[2].grant(provisioned[0], other_id, Grant(role="developer", scope=scope))
    denied = await author[0].get(url, headers=other_headers)
    assert denied.status_code == 403
    denied = await author[0].post(url + "/commands", headers={**other_headers, "If-Match": "1"}, json={"kind": "next"})
    assert denied.status_code == 403
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='developer'"), {"id": author[1].id}
        )
    assert (await author[0].get(url, headers=headers)).status_code == 403
    assert (
        await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
    ).status_code == 403
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT revision FROM debug_sessions")) == 1


async def test_real_db_expiry_does_not_follow_virtual_time(
    author, headers, project_url, worker_runtime_fixture, access_db
):
    from sqlalchemy import text

    url, created = await created_session(author, headers, project_url, worker_runtime_fixture["artifact"])
    moved = await author[0].post(
        url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "advance_time", "seconds": 366 * 86400}
    )
    assert moved.status_code == 200
    assert moved.json()["expires_at"] == created["expires_at"]
    async with access_db[1].begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:'||:t||':'||:p,0))"),
            {"t": str(author[2].tenant_id), "p": str(author[2].project_id)},
        )
        await session.execute(text("UPDATE debug_sessions SET expires_at=clock_timestamp()-interval '1 second'"))
    assert (await author[0].get(url, headers=headers)).status_code == 410
    assert (
        await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json={"kind": "next"})
    ).status_code == 410
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT revision FROM debug_sessions")) == 2


async def test_concurrent_commands_lock_and_fresh_service_restore(
    author, headers, project_url, worker_runtime_fixture, access_db, services
):
    import asyncio
    from uuid import UUID

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.access import Scope
    from firefly_weave.operations.debug.store import DebugService
    from firefly_weave.persistence.uow import UnitOfWork

    url, created = await created_session(author, headers, project_url, worker_runtime_fixture["artifact"])
    responses = await asyncio.gather(
        *(
            author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in responses) in ([200, 409], [200, 429])
    retry = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
    assert retry.status_code == 409
    service = services(access_db[0]).resolve(DebugService)
    scope = Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)
    async with UnitOfWork(access_db[0]).open(scope) as tx:
        restored = await service.inspect(tx, UUID(created["id"]), actor=author[1], scope=scope, context=AuditContext())
    assert restored.revision == 2 and len(restored.view.events) == 1


async def test_api_library_cli_trace_parity(author, headers, project_url, worker_runtime_fixture, tmp_path):
    import json
    from datetime import UTC, datetime

    from click.testing import CliRunner

    from firefly_weave.cli.main import cli
    from firefly_weave.operations.debug.models import DebugCommand
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = worker_runtime_fixture["artifact"]
    url, created = await created_session(author, headers, project_url, artifact)
    commands = [
        {"kind": "breakpoints", "node_ids": ["work"]},
        {"kind": "continue"},
        {"kind": "next"},
        {"kind": "continue"},
    ]
    simulator = Simulator(artifact, mocks={"node:work": 8}, input=3, now=datetime(2026, 1, 1, tzinfo=UTC))
    for revision, command in enumerate(commands, 1):
        response = await author[0].post(url + "/commands", headers={**headers, "If-Match": str(revision)}, json=command)
        assert response.status_code == 200, response.text
        local = simulator.command(DebugCommand.model_validate(command))
        assert response.json()["view"] == local.model_dump(mode="json")
    request_path, commands_path = tmp_path / "request.json", tmp_path / "commands.json"
    request_path.write_text(
        json.dumps(
            {
                "artifact": json.loads(artifact.to_bytes()),
                "mocks": {"node:work": 8},
                "input": 3,
                "now": "2026-01-01T00:00:00Z",
            }
        )
    )
    commands_path.write_text(json.dumps(commands))
    result = CliRunner().invoke(
        cli, ["workflow", "simulate", str(request_path), "--commands", str(commands_path), "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == response.json()["view"]


async def test_debug_needs_only_simulate_and_unsafe_payload_not_persisted(
    author, headers, project_url, worker_runtime_fixture, access_db
):
    from sqlalchemy import text

    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role!='developer'"), {"id": author[1].id}
        )
    url, created = await created_session(author, headers, project_url, worker_runtime_fixture["artifact"])
    unsafe = await author[0].post(
        url + "/commands",
        headers={**headers, "If-Match": "1"},
        json={"kind": "signal", "name": "unknown", "payload": "private-canary"},
    )
    assert unsafe.status_code == 422 and "private-canary" not in unsafe.text
    async with access_db[1]() as session:
        state = await session.scalar(text("SELECT state::text FROM debug_sessions"))
        assert "private-canary" not in state
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM task_intents")) == 0
        assert await session.scalar(text("SELECT count(*) FROM activation_revisions")) == 0


async def test_capacity_error_preserves_checkpoint_and_matches_local_cli(
    author, headers, project_url, worker_runtime_fixture, access_db, tmp_path
):
    import json

    from click.testing import CliRunner
    from sqlalchemy import text

    from firefly_weave.cli.main import cli
    from firefly_weave.operations.debug.simulator import Simulator

    url, created = await created_session(author, headers, project_url, worker_runtime_fixture["artifact"])
    async with access_db[1].begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:'||:t||':'||:p,0))"),
            {"t": str(author[2].tenant_id), "p": str(author[2].project_id)},
        )
        await session.execute(text("UPDATE debug_sessions SET state=jsonb_set(state,'{command_count}','10000')"))
        before = await session.scalar(text("SELECT state FROM debug_sessions"))
    simulator = Simulator.restore(json.dumps(before))
    with pytest.raises(ValueError, match="WV-DEBUG-LIMIT"):
        simulator.next()
    response = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "next"})
    assert response.status_code == 422 and response.json()["code"] == "WV-DEBUG-LIMIT"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state FROM debug_sessions")) == before
        assert await session.scalar(text("SELECT revision FROM debug_sessions")) == 1
    request_path, command_path = tmp_path / "request.json", tmp_path / "commands.json"
    request_path.write_text(
        json.dumps(
            {"artifact": before["artifact"], "mocks": before["mocks"], "input": 3, "now": "2026-01-01T00:00:00Z"}
        )
    )
    command_path.write_text(json.dumps([{"kind": "next"}] * 10001))
    result = CliRunner().invoke(
        cli, ["workflow", "simulate", str(request_path), "--commands", str(command_path), "--output", "json"]
    )
    assert result.exit_code == 1 and json.loads(result.output)["diagnostics"][0]["code"] == "WV-DEBUG-LIMIT"


async def test_exact_artifact_envelope_pinned_and_expiry_columns_immutable(
    author, headers, project_url, worker_runtime_fixture, access_db
):
    import json
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork

    envelope = json.loads(worker_runtime_fixture["artifact"].to_bytes())
    for span in envelope["sourceMap"].values():
        span["file"] = "/etc/passwd"
    response = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json={"artifact": envelope, "mocks": {}, "input": 3, "now": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert (
        datetime.fromisoformat(value["expires_at"]) - datetime.fromisoformat(value["created_at"])
    ).total_seconds() == 3600
    async with access_db[1]() as session:
        assert (await session.scalar(text("SELECT state FROM debug_sessions")))["artifact"] == envelope
        assert await session.scalar(text("SELECT relforcerowsecurity FROM pg_class WHERE relname='debug_sessions'"))
    scope = Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)
    with pytest.raises(DBAPIError):
        async with UnitOfWork(access_db[0]).open(scope) as tx:
            await tx.session.execute(
                text("UPDATE debug_sessions SET expires_at=clock_timestamp()+interval '1 day' WHERE id=:id"),
                {"id": UUID(value["id"])},
            )


async def test_unicode_checkpoint_restores_with_same_serialized_byte_budget(author, headers, project_url):
    import json

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    result = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "unicode-budget", "version": "1.0.0"},
            "spec": {
                "inputSchema": {},
                "outputSchema": {},
                "steps": [{"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(120)],
                "output": {"literal": True},
            },
        },
        format="object",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok
    response = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json={
            "artifact": json.loads(result.artifact.to_bytes()),
            "mocks": {},
            "input": "🙂" * 80000,
            "now": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201
    url = project_url + "/debug/sessions/" + response.json()["id"]
    breakpoint_response = await author[0].post(
        url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "breakpoints", "node_ids": ["copy50"]}
    )
    assert breakpoint_response.status_code == 200
    paused = await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json={"kind": "continue"})
    assert paused.status_code == 200
    assert paused.json()["view"]["status"] == "paused"
    restored = await author[0].get(url, headers=headers)
    assert restored.status_code == 200, restored.text[:200]
    assert restored.json() == paused.json()
    exceeded = await author[0].post(url + "/commands", headers={**headers, "If-Match": "3"}, json={"kind": "continue"})
    assert exceeded.status_code == 422 and exceeded.json()["code"] == "WV-DEBUG-LIMIT"
    unchanged = await author[0].get(url, headers=headers)
    assert unchanged.status_code == 200 and unchanged.json() == paused.json()
