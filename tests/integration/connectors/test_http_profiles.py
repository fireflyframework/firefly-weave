# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Generated native HTTP package against owned TLS sockets, including lost acknowledgments."""

import asyncio
import importlib
import importlib.util
import os
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.context import ApplicationContext
from pyfly.core.config import Config

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connectors.egress import SecureHttpClient
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector, register_http_profile_services
from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorFailure, ConnectorInvocation
from firefly_weave.sdk.connectors import package, scaffold_import, validate
from firefly_weave.sdk.openapi_import import import_openapi


async def test_generated_native_package_tls_and_lost_ack(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    key, cert = tmp_path / "fixture.key", tmp_path / "fixture.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(cert))
    received = []

    async def handle(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            received.append(request)
            if request.startswith(b"POST"):
                return
            body = b'{"name":"fixture"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    origin = f"https://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    response = {"200": {"description": "fixture", "content": {"application/json": {"schema": schema}}}}
    document = {
        "openapi": "3.1.1",
        "info": {"title": "fixture", "version": "1"},
        "servers": [{"url": origin}],
        "security": [{"token": []}],
        "components": {"securitySchemes": {"token": {"type": "http", "scheme": "bearer"}}},
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}}
                ],
                "get": {"operationId": "fetch", "responses": response},
                "post": {"operationId": "write", "responses": response},
            }
        },
    }
    rules = {
        "name": "e8-http-fixture",
        "version": "1.0.0",
        "auth": {"kind": "bearer"},
        "operations": {
            "fetch": {"name": "fetch", "sideEffect": "read_only", "server": origin, "statuses": [200]},
            "write": {"name": "write", "sideEffect": "non_idempotent", "server": origin, "statuses": [200]},
        },
    }
    result = import_openapi(document, ["fetch", "write"], rules)
    assert result.ok, result.diagnostics
    target = tmp_path / "generated"
    scaffold_import(target, result)
    metadata = validate(target / "connector.json")
    if os.environ.get("WEAVE_E8_INSTALLED_GATE") == "1":
        # Only the explicit clean-environment artifact gate performs this local build/install.
        dist = tmp_path / "dist"
        await asyncio.to_thread(package, target, dist)
        wheel = next(dist.glob("*.whl"))
        install = await asyncio.create_subprocess_exec(
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            str(wheel),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await install.communicate()
        assert install.returncode == 0, (stdout + stderr).decode()
        importlib.invalidate_caches()
        mod = importlib.import_module("e8_http_fixture")
        assert "site-packages" in mod.__file__ and str(target) not in mod.__file__
        identity = "e8-http-fixture:e8-http-fixture:e8_http_fixture:package"
        registry = ConnectorRegistry((identity,))
        assert registry.packages[0].metadata.canonical.digest == metadata.canonical.digest
    else:
        spec = importlib.util.spec_from_file_location(
            "e8_http_fixture",
            target / "src/e8_http_fixture/__init__.py",
            submodule_search_locations=[str(target / "src/e8_http_fixture")],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["e8_http_fixture"] = mod
        spec.loader.exec_module(mod)
        registry = ConnectorRegistry()

    class FixtureTLSClient(SecureHttpClient):
        async def request_bounded(self, method, url, **kwargs):
            return await super().request_bounded(method, url, tls=client_context, **kwargs)

    native = ApplicationContext(Config({}))
    native.container.register_instance(BoundedHttpClientPort, FixtureTLSClient())
    native.container.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    register_http_profile_services(native)
    native.register_bean(mod.ImportedHttpConnector)
    await native.start()
    try:
        adapter = native.get_bean(mod.ImportedHttpConnector)
        assert adapter.profile is native.get_bean(HttpProfileConnector)
        if registry.packages:
            registry.resolve_services(native)
            assert registry.get("e8-http-fixture") is adapter
        config = result.connector["spec"]["configSchema"]["const"]
        connection = ConnectionRevision(
            id=uuid4(),
            revision=1,
            name="fixture",
            connector_version_id=uuid4(),
            connector="e8-http-fixture@1.0.0",
            connector_digest=metadata.descriptor.manifest.digest,
            adapter="e8-http-fixture",
            config=config,
            secretRef={"token": "fixture"},
            allowed_destinations=(origin,),
        )
        catalog = CatalogSnapshot.from_definitions(
            [metadata.model.manifest], tasks=metadata.model.capabilities, adapters=["e8-http-fixture"]
        )

        from firefly_weave.contracts.definitions import ActionDefinition

        workflow_catalog = CatalogSnapshot.from_definitions(
            [metadata.model.manifest, *[ActionDefinition.model_validate(a) for a in result.actions]],
            tasks=metadata.model.capabilities,
            adapters=["e8-http-fixture"],
        )
        fetch = next(a for a in result.actions if a["metadata"]["name"] == "fetch")
        workflow = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "http-fixture-workflow", "version": "1.0.0"},
            "spec": {
                "connections": {"api": {"connector": "e8-http-fixture@1.0.0"}},
                "inputSchema": fetch["spec"]["inputSchema"],
                "outputSchema": fetch["spec"]["outputSchema"],
                "steps": [
                    {
                        "id": "fetch",
                        "kind": "action",
                        "uses": "fetch@1.0.0",
                        "connection": "api",
                        "with": {"ref": "/input"},
                    }
                ],
                "output": {"ref": "/steps/fetch/output"},
            },
        }
        compiled = compile_source(workflow, format="object", catalog=workflow_catalog)
        assert compiled.ok, compiled.diagnostics

        async def authorize():
            pass

        async def credentials(slot):
            assert slot == "token"
            return ResolvedSecret(value="TLS-CANARY")

        for action in result.actions:
            assert compile_source(action, format="object", catalog=catalog).ok
            spec = action["spec"]
            invocation = ConnectorInvocation(
                connection,
                spec["implementation"]["config"],
                action["metadata"]["name"],
                spec["inputSchema"],
                spec["outputSchema"],
                1048576,
                1048576,
            )
            context = ActionContext(
                "installed-op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize
            )
            if action["metadata"]["name"] == "fetch":
                assert await adapter.execute({"path": {"id": "a/b?c"}}, context) == {
                    "status": 200,
                    "body": {"name": "fixture"},
                }
            else:
                with pytest.raises(ConnectorFailure) as error:
                    await adapter.execute({"path": {"id": "a/b?c"}}, context)
                assert error.value.outcome == "unknown"
        assert "TLS-CANARY" not in caplog.text
        assert len(received) == 2
        assert received[0].startswith(b"GET /items/a%2Fb%3Fc HTTP/1.1")
        assert b"Authorization: Bearer TLS-CANARY" in received[0]
        assert received[1].startswith(b"POST /items/a%2Fb%3Fc HTTP/1.1")
    finally:
        await native.stop()
        listener.close()
        await listener.wait_closed()
        sys.modules.pop("e8_http_fixture", None)
