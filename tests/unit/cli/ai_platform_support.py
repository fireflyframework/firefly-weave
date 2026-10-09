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

"""Shared fixtures for the local AI platform tests: an owned Docker installation and a recording runner."""

import json
import shlex

import pytest

from firefly_weave import __version__
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.sdk import platform

OWNER = "c" * 24
SERVER_IMAGE = "sha256:" + "d" * 64
SUBNET = "10.246.21.0/24"
SCOPE = {
    "tenant_id": "11111111-1111-4111-8111-111111111111",
    "project_id": "22222222-2222-4222-8222-222222222222",
    "environment_id": "33333333-3333-4333-8333-333333333333",
}
WORKER_SECRET = "worker-client-secret-canary"
ADMIN_SECRET = "keycloak-admin-secret-canary"


class Runner:
    """Records Docker commands by stage name and answers the ones the AI commands read."""

    def __init__(self):
        self.calls = []
        self.answers = {"image-check": SERVER_IMAGE.encode()}

    def __call__(self, state, name, command, **kwargs):
        self.calls.append((name, list(command)))
        answer = self.answers.get(name, b"")
        if isinstance(answer, BaseException):
            raise answer
        return answer(command) if callable(answer) else answer

    def names(self):
        return [name for name, _ in self.calls]


@pytest.fixture(name="owned")
def owned_fixture(tmp_path, monkeypatch):
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    state = {
        "format": platform._FORMAT,
        "id": OWNER,
        "directory": str(directory),
        "source": str(tmp_path),
        "source_sha256": "fingerprint",
        "version": __version__,
        "stage": "ready",
        "mode": "docker",
        "context": "colima-weave-tests",
        "endpoint": "unix:///owned.sock",
        "engine": "owned-engine",
        "subnet": SUBNET,
        "docker_image": SERVER_IMAGE,
        "ports": {"postgres": 55201, "keycloak": 55202, "api": 55203, "container_api": 55204},
    }
    platform._write(directory / "platform.json", state)
    override = directory / "compose.network.yaml"
    override.write_bytes(platform._network_override(state))
    override.chmod(0o600)
    issuer = "http://localhost:55202/realms/weave"
    runtime = {
        "WEAVE_DATABASE_URL": "postgresql+asyncpg://weave_runtime_t:app@localhost:55201/weave_b2_dev_t",
        "WEAVE_SCHEDULER_DATABASE_URL": "postgresql+asyncpg://weave_scheduler_t:sch@localhost:55201/weave_b2_dev_t",
        "WEAVE_OIDC_PROVIDERS": json.dumps(
            [
                {
                    "provider_id": "local-keycloak",
                    "issuer": issuer,
                    "jwks_uri": issuer + "/protocol/openid-connect/certs",
                    "local_development": True,
                    "audience": "weave-api",
                    "clients": {"weave-cli": "human"},
                }
            ]
        ),
    }
    identity = {
        "WEAVE_HOST_SECRET": "host-secret",
        "WEAVE_KC_ADMIN_SECRET": ADMIN_SECRET,
        "WEAVE_WORKER_SECRET": WORKER_SECRET,
    }
    for name, values in {
        "runtime.env": runtime,
        "identity.env": identity,
        "postgres.env": {"WEAVE_POSTGRES_PASSWORD": "owner-secret"},
    }.items():
        path = directory / name
        path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
        path.chmod(0o600)
    monkeypatch.setattr(platform, "_source", lambda path: (path, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda state: None)
    monkeypatch.setattr(platform, "_check_subnet", lambda context, value: value)
    runner = Runner()
    monkeypatch.setattr(platform, "_run", runner)
    return directory, state, runner


def catalog_output():
    """What the Agentic worker image prints for --catalog."""
    definitions = [
        load_definition(value).model_dump(by_alias=True)
        for value in (AGENTIC_DESCRIPTOR.manifest.value, action_definition())
    ]
    return json.dumps(
        {
            "definitions": [
                {"document": value, "digest": FrozenDocument.from_value(value).digest} for value in definitions
            ],
            "tasks": [task_capability().model_dump(by_alias=True)],
            "adapters": ["weave-agentic-provider"],
            "schemas": {},
        }
    ).encode()


def manifest_output():
    """What the Agentic worker image prints for --release-manifest."""
    return json.dumps(
        {
            "capabilities": [task_capability().model_dump(by_alias=True)],
            "credential_capabilities": ["weave-agentic.generate@1.0.0"],
        }
    ).encode()
