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

"""Runner configuration cannot silently reuse an operator's interactive login."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.deployment_runner.application import RunnerConfiguration, load_configuration


def configuration():
    return {
        "base_url": "https://weave.example",
        "scope": {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())},
        "oauth_file": "/run/secrets/weave-runner-oauth.json",
        "destination": {
            "target_id": str(uuid4()),
            "adapter": "kubernetes",
            "external_identity": "namespace-uid",
            "boundary": "weave",
            "executable": "/usr/local/bin/kubectl",
            "context": "cluster",
            "capabilities": ["observe"],
            "components": [{"name": "worker", "container": "worker", "kind": "worker", "configuration": "current"}],
            "image_repositories": ["registry.example/worker"],
        },
    }


def test_config_requires_separate_machine_auth_and_full_scope(tmp_path):
    data = configuration()
    path = tmp_path / "runner.json"
    path.write_text(json.dumps(data))
    assert load_configuration(path).destination.boundary == "weave"
    for changes in [
        {"oauth_file": None},
        {"token_file": "/tmp/token"},
        {"base_url": "http://remote.invalid"},
        {"scope": {"tenant_id": str(uuid4())}},
        {"oauth_file": "relative.json"},
    ]:
        with pytest.raises(ValidationError):
            RunnerConfiguration.model_validate_json(json.dumps({**data, **changes}))


def test_configuration_file_is_bounded(tmp_path):
    path = tmp_path / "runner.json"
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(ValueError):
        load_configuration(path)


def test_plain_http_reaches_the_api_only_through_a_runner_entry():
    """In a cluster the runner reaches the platform over plain HTTP on the cluster network, approved by an exact
    runner entry. A platform-api entry alone is not enough, and loopback keeps working without any entry."""
    from firefly_weave import private_origins as po

    origin = "http://weave-api.weave-system.svc:8000"
    data = json.dumps({**configuration(), "base_url": origin})

    def entry(purpose):
        return po.PrivateOrigin(origin=origin, purpose=purpose, networks=("10.89.0.0/24",), credentials="bridge")

    with po.installed(po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry("runner")])):
        assert RunnerConfiguration.model_validate_json(data).base_url == origin
    for policy in (
        po.PrivateOrigins.empty(),
        po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry("platform-api")]),
    ):
        with po.installed(policy), pytest.raises(ValidationError, match="Use one mounted machine identity"):
            RunnerConfiguration.model_validate_json(data)
    with po.installed(po.PrivateOrigins.empty()):
        loopback = json.dumps({**configuration(), "base_url": "http://127.0.0.1:8000"})
        assert RunnerConfiguration.model_validate_json(loopback).base_url == "http://127.0.0.1:8000"
