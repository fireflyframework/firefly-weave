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

"""A runner independently constrains API plans to its operator-owned destination."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.deployment_runner.policy import DestinationPolicy


def policy(**changes):
    return {
        "target_id": str(uuid4()),
        "adapter": "kubernetes",
        "external_identity": "dev-cluster",
        "boundary": "weave",
        "executable": "/usr/local/bin/kubectl",
        "context": "dev",
        "capabilities": ["observe", "update", "scale_workers"],
        "components": [{"name": "worker", "kind": "worker", "configuration": "current", "container": "worker"}],
        "image_repositories": ["registry.example/weave/worker"],
        **changes,
    }


def test_operator_policy_accepts_explicit_destination_and_image_repository():
    value = DestinationPolicy.model_validate_json(__import__("json").dumps(policy()))
    assert value.allows_image("registry.example/weave/worker@sha256:" + "a" * 64)
    assert not value.allows_image("registry.example/weave/worker-evil@sha256:" + "a" * 64)
    assert not value.allows_image("registry.example/weave/worker:latest")


@pytest.mark.parametrize(
    "change",
    [
        {"executable": "kubectl"},
        {"boundary": "--namespace=other"},
        {"capabilities": ["scale_workers"]},
        {"capabilities": ["observe", "observe"]},
        {"components": [{"name": "api", "kind": "api", "configuration": "current"}] * 2},
        {"image_repositories": ["registry.example/../other"]},
        {"capabilities": ["observe", "drain_workers"]},
    ],
)
def test_unsafe_or_unimplemented_policy_is_rejected(change):
    with pytest.raises(ValidationError):
        DestinationPolicy.model_validate_json(__import__("json").dumps(policy(**change)))
