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

"""Artifact validation rejects source substitutions before native startup."""

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "e2e/support/installed_api.py"
spec = importlib.util.spec_from_file_location("weave_installed_launcher_tests", MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_wheel_hash_is_checked_before_import(tmp_path):
    wheel = tmp_path / "fake.whl"
    wheel.write_bytes(b"not a wheel")
    with pytest.raises(module.ArtifactError, match="identity"):
        module.verify_install(wheel, "0" * 64)


def test_configuration_rejects_unknown_boundary_and_nonabsolute_path():
    with pytest.raises(ValueError):
        module.validate_target({"phase": "fake"})
    with pytest.raises(ValueError):
        module.validate_target({"phase": "before_run_commit", "case": "fake", "path": "relative"})


def test_teams_fixture_preserves_real_owned_outbox_transport(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from firefly_weave.connectors.egress import EgressDenied, EgressPolicy

    class Container:
        def register_instance(self, key, instance):
            self.client = instance

    async def exercise():
        requests = []

        async def receive(reader, writer):
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(receive, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        owned = f"http://127.0.0.1:{port}"
        keys = tmp_path / "jwks.json"
        keys.write_text('{"keys":[]}')
        container = Container()
        app = SimpleNamespace(
            state=SimpleNamespace(pyfly=SimpleNamespace(context=SimpleNamespace(container=container)))
        )
        try:
            module.inject_local_teams_jwks(app, keys, receiver_origin=owned)
            client = container.client
            jwks = await client.request_bounded("GET", "https://login.botframework.com/v1/.well-known/keys")
            assert jwks.content == keys.read_bytes()
            response = await client.request_bounded(
                "POST",
                owned + "/effect",
                content=b"",
                max_response_bytes=16,
                timeout=2,
                egress_policy=EgressPolicy((owned,), ("127.0.0.0/8",)),
            )
            assert response.status_code == 200 and len(requests) == 1
            for url in (f"http://127.0.0.1:{port + 1}/effect", "https://example.com/effect"):
                with pytest.raises(RuntimeError, match="forbids"):
                    await client.request_bounded("POST", url, max_response_bytes=16)
            with pytest.raises(EgressDenied):
                await client.request_bounded(
                    "POST",
                    owned + "/effect",
                    content=b"",
                    max_response_bytes=16,
                    timeout=2,
                    egress_policy=EgressPolicy((owned,)),
                )
            assert len(requests) == 1
            module.inject_local_teams_jwks(app, keys)
            with pytest.raises(RuntimeError, match="forbids"):
                await container.client.request_bounded("POST", owned + "/effect", max_response_bytes=16)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())
