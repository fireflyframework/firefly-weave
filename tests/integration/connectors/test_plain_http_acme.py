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

"""A no-code HTTP connection calls the Acme fixture over plain HTTP, with its API key, only where C8 allows."""

import hashlib
import importlib.util
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connectors.egress import SecureHttpClient
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.connectors import (
    ActionContext,
    ConnectionRequest,
    ConnectionRevision,
    ConnectorFailure,
    ConnectorInvocation,
    transport_encrypted,
)
from firefly_weave.sdk.http_actions import build_connection_request

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
KEY = "wv-canary-acme-key"


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "acme_api_fixture_plain_http", ROOT / "tests/acceptance/fixtures/acme_api/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acme = load_fixture()


@pytest.fixture
def fixture_api():
    api, control, state = acme.make_servers(
        host="127.0.0.1", api_port=0, control_port=0, key_sha256=hashlib.sha256(KEY.encode()).hexdigest()
    )
    for server in (api, control):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{api.server_address[1]}", state
    for server in (api, control):
        server.shutdown()
        server.server_close()


def connection(origin):
    body = build_connection_request(
        "acme", origin, {"kind": "api-key", "header": "X-Api-Key"}, {"api_key": "acme-api-key"}, uuid4()
    )
    request = ConnectionRequest.model_validate_json(json.dumps(body))
    return ConnectionRevision(
        **request.model_dump(by_alias=True),
        id=uuid4(),
        revision=1,
        connector="weave-http@2.0.0",
        connector_digest="a" * 64,
        adapter="weave-http-v2",
    )


def read_customer(revision):
    async def credentials(slot):
        return ResolvedSecret(value=KEY)

    async def authorize():
        return None

    config = {
        "profileVersion": "2.0.0",
        "method": "GET",
        "path": "/customers/{id}",
        "sideEffect": "read_only",
        "statuses": [200],
        "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
    }
    invocation = ConnectorInvocation(revision, config, "read", {"type": "object"}, {"type": "object"}, 1048576, 1048576)
    return ActionContext("op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize)


async def test_the_acme_fixture_is_reached_over_plain_http_only_inside_an_approved_network(fixture_api):
    origin, state = fixture_api
    revision = connection(origin)
    assert revision.config["baseUrl"] == origin and transport_encrypted(revision.config) is False
    # Loopback is private: with no entry and no legacy network, nothing reaches the fixture.
    with pytest.raises(ConnectorFailure) as refused:
        await HttpProfileConnector(SecureHttpClient(), HttpPolicy(), None).execute(
            {"path": {"id": "C-1001"}}, read_customer(revision)
        )
    assert refused.value.code == "HTTP_PROFILE_DESTINATION" and state.journal == []
    # A file entry can never open loopback for connector clients (C8 control plane), so this in-process
    # fixture uses the legacy network setting; J0 covers the egress network with a file entry (Task 16).
    result = await HttpProfileConnector(SecureHttpClient(), HttpPolicy(("127.0.0.0/8",)), None).execute(
        {"path": {"id": "C-1001"}}, read_customer(revision)
    )
    assert result["status"] == 200 and result["body"]["id"] == "C-1001"
    # The API key traveled in plain text, as the "Not encrypted" warning says; the fixture keeps only its hash.
    assert [(entry["method"], entry["path"], entry["key_sha256"]) for entry in state.journal] == [
        ("GET", "/customers/C-1001", hashlib.sha256(KEY.encode()).hexdigest())
    ]
