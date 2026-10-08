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

"""Shared local catalog and runtime fixtures for pure and contract tests."""

from pathlib import Path

import pytest

# pytest exports each running test's node ID as PYTEST_CURRENT_TEST ("<node ID> (teardown)"),
# and Windows refuses an environment variable whose "NAME=value" exceeds 32,767 UTF-16 units.
# Checking the IDs on every platform stops a long generated ID from failing only on Windows.
WINDOWS_ENVIRONMENT_LIMIT = 32_767


def windows_environment_length(nodeid: str) -> int:
    """UTF-16 length of the longest PYTEST_CURRENT_TEST assignment pytest makes for ``nodeid``."""
    return len(f"PYTEST_CURRENT_TEST={nodeid} (teardown)".encode("utf-16-le")) // 2


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    too_long = [item.nodeid for item in items if windows_environment_length(item.nodeid) > WINDOWS_ENVIRONMENT_LIMIT]
    if too_long:
        named = "\n".join(f"  {nodeid[:100]}..." for nodeid in too_long)
        raise pytest.UsageError(
            f"Give these parametrized tests short explicit IDs; Windows can't run tests with IDs this long:\n{named}"
        )


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def workflow_source() -> str:
    return Path("examples/definitions/customer-onboarding.workflow.yaml").read_text()


@pytest.fixture
def catalog():
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.compiler.parser import parse_source

    lock = parse_source(Path("tests/fixtures/catalog/onboarding.lock.json").read_text(), format="json")
    return CatalogSnapshot.from_lock(lock.value)


@pytest.fixture
def worker_runtime_fixture():
    """Trusted kernel/storage fixture only; worker admission must create an admitted release for live starts."""
    import json
    from uuid import UUID

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    contract = {
        "taskType": "echo",
        "taskVersion": "1.2.0",
        "inputSchema": {},
        "outputSchema": {"type": "integer"},
        "sideEffect": "read_only",
        "timeoutSeconds": 30,
    }
    action = load_definition(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": "echo-action", "version": "2.0.0"},
            "spec": {
                "implementation": {"kind": "worker", "taskType": "echo", "taskVersion": "1.2.0"},
                "inputSchema": {},
                "outputSchema": {"type": "integer"},
                "sideEffect": "read_only",
                "timeoutSeconds": 30,
            },
        }
    )
    catalog = CatalogSnapshot.from_definitions([action], tasks=[contract])
    source = json.dumps(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "worker-flow", "version": "1.0.0"},
            "spec": {
                "inputSchema": {},
                "outputSchema": {},
                "steps": [{"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}],
                "output": {"ref": "/steps/work/output"},
            },
        }
    )
    result = compile_source(source, format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return {
        "artifact": result.artifact,
        "catalog": catalog,
        "action": action,
        "source": source,
        "task_type": "echo",
        "task_version": "1.2.0",
        "release_id": UUID("b5000000-0000-4000-8000-000000000006"),
    }


@pytest.fixture
def parallel_artifact(worker_runtime_fixture):
    import json

    from firefly_weave.compiler.api import compile_source

    document = json.loads(worker_runtime_fixture["source"])
    document["spec"]["steps"] = [
        {
            "id": "fork",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                n: {
                    "steps": [{"id": n, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}],
                    "output": {"ref": f"/steps/{n}/output"},
                }
                for n in ("a", "b")
            },
        }
    ]
    document["spec"]["output"] = {"ref": "/steps/fork/output"}
    result = compile_source(json.dumps(document), format="json", catalog=worker_runtime_fixture["catalog"])
    assert result.ok, result.to_bytes()
    return result.artifact


@pytest.fixture
def parallel_state(parallel_artifact):
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    return transition(
        RunState(input=1),
        RuntimeEvent(id=uuid4(), type="started", timestamp=datetime(2026, 9, 30, tzinfo=UTC), sequence=1),
        parallel_artifact,
    ).state


@pytest.fixture
def completion_events():
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.models import RuntimeEvent

    return [
        RuntimeEvent(
            id=uuid4(),
            type="task_completed",
            timestamp=datetime(2026, 9, 30, tzinfo=UTC),
            sequence=2 + i,
            data={"node_id": name, "output": i + 1},
        )
        for i, name in enumerate(("a", "b"))
    ]


@pytest.fixture
def signed_activity():
    import base64
    import json
    import time

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    n = key.public_key().public_numbers()
    jwks = json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "fixture",
                    "alg": "RS256",
                    "use": "sig",
                    "n": enc(n.n.to_bytes(256, "big")),
                    "e": enc(n.e.to_bytes(3, "big")),
                }
            ]
        }
    ).encode()
    from uuid import uuid4

    data = dict(
        account_id=str(uuid4()),
        registration_tenant=str(uuid4()),
        tenant_id=str(uuid4()),
        conversation_id="conv/1",
        bot_id="28:bot",
        user_id="29:user",
        installation_generation=1,
        allowed_tenants=[],
    )
    data["allowed_tenants"] = [data["tenant_id"]]
    activity = {
        "type": "message",
        "id": "activity-1",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "conversation": {"id": data["conversation_id"], "conversationType": "personal", "tenantId": data["tenant_id"]},
        "from": {"id": data["user_id"]},
        "recipient": {"id": data["bot_id"]},
        "channelData": {"tenant": {"id": data["tenant_id"]}},
        "text": "hello",
        "textFormat": "plain",
    }

    def token(_headers=None, **changes):
        now = int(time.time())
        claims = {
            "iss": "https://api.botframework.com",
            "aud": data["account_id"],
            "iat": now,
            "nbf": now,
            "exp": now + 600,
            "serviceurl": activity["serviceUrl"],
            **changes,
        }
        content = (
            enc(json.dumps({"alg": "RS256", "kid": "fixture"} if _headers is None else _headers).encode())
            + "."
            + enc(json.dumps(claims).encode())
        )
        return content + "." + enc(key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256()))

    return data, activity, token, jwks
