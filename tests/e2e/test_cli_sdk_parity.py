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

"""Thin public CLI command registration and safe machine output."""

import json

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_public_command_families_exist():
    expected = {"auth", "definitions", "connections", "runs", "workers", "triggers", "remote"}
    assert expected <= set(cli.commands)


def test_remote_usage_json_is_a_single_safe_envelope():
    result = CliRunner().invoke(cli, ["remote", "compile", "--output", "json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["code"] == "WV-CLI-USAGE"
    assert "Traceback" not in result.output


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="module")
async def test_installed_sdk_cli_native_public_journey(vertical_slice, tmp_path):
    import asyncio
    import os
    from datetime import UTC, datetime
    from pathlib import Path
    from uuid import UUID, uuid4

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.definitions import load_definition
    from firefly_weave.contracts.runtime import SignalRequest, StartRunRequest
    from firefly_weave.contracts.workers import ReleaseRequest
    from firefly_weave.operations.debug.models import DebugCommand, DebugCreate
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import PreconditionFailed

    live = vertical_slice
    await live.bootstrap()
    live.api_env["WEAVE_HTTP_PRIVATE_NETWORKS"] = '["127.0.0.0/8"]'
    live.api.terminate()
    await live.api.wait()
    await live.start_api()
    scope = live.scope
    environment = {
        **os.environ,
        "WEAVE_BASE_URL": live.api_url,
        "WEAVE_ACCESS_TOKEN": live.tokens[0],
        "WEAVE_TENANT_ID": str(scope.tenant_id),
        "WEAVE_PROJECT_ID": str(scope.project_id),
        "WEAVE_ENVIRONMENT_ID": str(scope.environment_id),
    }

    async def cli_call(arguments, expected=0, *, env=None):
        process = await asyncio.create_subprocess_exec(
            live.python,
            "-m",
            "firefly_weave.cli.main",
            *arguments,
            "--output",
            "json",
            env=env or environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == expected, (process.returncode, stdout.decode())
        assert live.tokens[0].encode() not in stdout + stderr
        value = json.loads(stdout)
        assert b"Traceback" not in stdout + stderr
        return value, stderr

    def request_file(name, value):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        return str(path)

    source = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: sdk-clock, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  steps:
    - {id: pause, kind: wait, durationSeconds: 1}
    - {id: value, kind: transform, value: {literal: 7}}
  output: {ref: /steps/value/output}
"""
    # A signalled workflow provides two evidence events; simulation uses virtual time.
    pure = source.replace(
        "    - {id: pause, kind: wait, durationSeconds: 1}",
        "    - {id: pause, kind: signal, name: approve, timeoutSeconds: 60, payloadSchema: {type: integer}}",
    )
    async with WeaveClient(live.api_url, lambda: live.tokens[0], scope) as sdk:
        lock = await sdk.catalog()
        local = compile_source(
            pure,
            format="yaml",
            catalog=CatalogSnapshot.from_lock(lock.model_dump(mode="json", by_alias=True)),
            filename="host.yaml",
        )
        remote = await sdk.compile(source=pure, format="yaml", catalog=lock, filename="host.yaml")
        assert remote.artifact.digest == local.artifact.digest
        compile_input = request_file(
            "compile",
            {
                "source": pure,
                "format": "yaml",
                "filename": "host.yaml",
                "catalog": lock.model_dump(mode="json", by_alias=True),
            },
        )
        cli_result, stderr = await cli_call(["remote", "compile", "--request", compile_input])
        assert cli_result == json.loads(local.to_bytes()) and not stderr
        invalid = request_file(
            "invalid",
            {
                "source": "kind: Unknown\n",
                "format": "yaml",
                "filename": "editor.yaml",
                "catalog": lock.model_dump(mode="json", by_alias=True),
            },
        )
        diagnostic, stderr = await cli_call(["remote", "compile", "--request", invalid], expected=1)
        assert diagnostic == json.loads(
            compile_source(
                "kind: Unknown\n",
                format="yaml",
                catalog=CatalogSnapshot.from_lock(lock.model_dump(mode="json", by_alias=True)),
                filename="editor.yaml",
            ).to_bytes()
        )
        assert stderr
        await cli_call(["remote", "compile"], expected=2)
        await cli_call(["remote", "catalog", "--base-url", "http://127.0.0.1:1"], expected=3)
        await cli_call(["remote", "catalog"], expected=1, env={**environment, "WEAVE_ACCESS_TOKEN": live.tokens[2]})
        draft_id = uuid4()
        draft = await sdk.save_draft(draft_id, {"unfinished": True})
        await sdk.save_draft(draft_id, {"unfinished": False}, revision=draft.revision)
        with pytest.raises(PreconditionFailed):
            await sdk.save_draft(draft_id, {}, revision=draft.revision)
        page = await sdk.list_definitions("drafts", limit=1)
        assert page.items[0].id == draft_id
        retired = await sdk.delete_draft(draft_id, revision=2)
        assert retired.retired and (await sdk.read_draft(draft_id)).retired
        assert len((await sdk.export_draft(draft_id)).revisions) == 2
        descriptor = json.loads(Path("examples/host_product/http-manifest.json").read_text())
        connector = await sdk.publish(
            "connectors", json.dumps(descriptor["connector"]), "json", idempotency_key="sdk-connector"
        )
        await sdk.register_release(
            ReleaseRequest.model_validate_json(
                json.dumps(
                    {
                        "image_digest": live.image,
                        "capabilities": descriptor["capabilities"],
                        "connector_bindings": descriptor["connector_bindings"],
                        "credential_capabilities": [descriptor["connector_bindings"][0]["task_reference"]],
                    }
                )
            )
        )
        request = ConnectionRequest(
            name="sdk-http",
            connector_version_id=connector.id,
            config={"baseUrl": f"http://127.0.0.1:{live.target_port}", "auth": "bearer"},
            secretRef={"token": "http-token"},
            allowed_destinations=(f"http://127.0.0.1:{live.target_port}",),
        )
        first = await sdk.create_connection(request)
        second = await sdk.create_connection(
            request.model_copy(update={"config": {**request.config, "auth": "none"}, "secret_refs": {}})
        )
        assert first.id != second.id and second.revision == first.revision + 1
        assert (await sdk.read_connection(first.id)).secret_refs == {"token": "http-token"}
        assert (await sdk.test_connection(first.id)).ok
        action_contract = descriptor["connector"]["spec"]["actions"]["read"]
        for version in ("1.0.0", "1.0.1"):
            action = {
                "apiVersion": "weave/v1alpha1",
                "kind": "Action",
                "metadata": {"name": "sdk-read", "version": version},
                "spec": {
                    "implementation": {
                        "kind": "connector",
                        "uses": "weave-http@1.0.0",
                        "action": "read",
                        "config": {"method": "GET", "path": "/customer", "statuses": [200]},
                    },
                    "connection": {"connector": "weave-http@1.0.0"},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                    "inputSchema": action_contract["inputSchema"],
                    "outputSchema": action_contract["outputSchema"],
                },
            }
            published = await sdk.publish(
                "actions", json.dumps(action), "json", idempotency_key="sdk-action-" + version
            )
            assert (await sdk.export_definition("actions", published.id)).document == load_definition(
                action
            ).model_dump(mode="json", by_alias=True)
        version = await sdk.publish("workflows", pure, "yaml", idempotency_key="sdk-run")
        activation = await sdk.activate(
            ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=scope),
            idempotency_key="sdk-activation",
        )
        run = await sdk.start_run(StartRunRequest(activation_id=activation.id, input=None), idempotency_key="sdk-start")
        await sdk.signal(run.id, SignalRequest(eventId="sdk-approve", name="approve", payload=1))
        trace = await sdk.history(run.id, limit=1)
        assert trace.events
        if trace.next_cursor:
            assert (await sdk.history(run.id, cursor=trace.next_cursor)).events
        assert (await sdk.replay(run.id)).status == "consistent"
        effects_before = dict(live.effects)
        replay, _ = await cli_call(["runs", "replay", str(run.id)])
        assert replay["status"] == "consistent" and live.effects == effects_before
        prefix, _ = await cli_call(["runs", "replay", str(run.id), "--limit", "1"], expected=1)
        assert prefix["status"] == "incomplete"
        compiled = await sdk.compile(source=source, format="yaml", catalog=lock)
        assert compiled.ok, compiled.to_bytes()
        debug = await sdk.create_debug(
            DebugCreate(artifact=json.loads(compiled.artifact.to_bytes()), now=datetime(2026, 1, 1, tzinfo=UTC))
        )
        debug = await sdk.command_debug(debug.id, DebugCommand(kind="breakpoints", node_ids=["pause"]), revision=1)
        command = request_file("continue", {"kind": "continue"})
        stopped, _ = await cli_call(
            ["runs", "debug", "command", str(debug.id), "--revision", str(debug.revision), "--request", command]
        )
        assert stopped["view"]["selected_node"] == "pause"
        debug = await sdk.command_debug(debug.id, DebugCommand(kind="next"), revision=stopped["revision"])
        debug = await sdk.command_debug(debug.id, DebugCommand(kind="advance_time", seconds=1), revision=debug.revision)
        debug = await sdk.command_debug(debug.id, DebugCommand(kind="continue"), revision=debug.revision)
        assert debug.view.status == "succeeded" and live.effects == effects_before
        assert (await sdk.capabilities()).wire_version == "weave/api-v1"
        assert "workflow" in await sdk.schemas()
        assert isinstance((await sdk.list_schedules()).items, list)
        assert (await sdk.run_incidents(UUID(str(run.id)))).items == []
