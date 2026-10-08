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

"""A run whose IR version or language features the platform does not run is skipped, never blocked."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import language_features
from firefly_weave.definitions.models import CatalogError
from firefly_weave.runtime import admission as policy
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.waits import settle

SCOPE = SimpleNamespace(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
GREETING = {"op": {"name": "concat", "args": [{"literal": "Hello "}, {"ref": "/input"}]}}


def envelope(output):
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greeting", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [], "output": output},
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return json.loads(result.artifact.to_bytes())


def run(artifact, **state):
    return {
        "id": uuid4(),
        "tenant_id": SCOPE.tenant_id,
        "project_id": SCOPE.project_id,
        "environment_id": SCOPE.environment_id,
        "state": {"status": "waiting", "accepted_sequence": 1, "admission_policy": "classified-v1", **state},
        "artifact": artifact,
    }


@pytest.fixture
def rolled_back(monkeypatch):
    """A platform rolled back to a release that lists no language features."""
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ())


@pytest.fixture
def policy_blocks(monkeypatch):
    observed = []

    async def observe_policy_block(self, row):
        observed.append(row["id"])

    monkeypatch.setattr(RuntimeRepository, "observe_policy_block", observe_policy_block)
    return observed


def test_a_run_the_platform_runs_is_available():
    assert policy.admission(run(envelope(GREETING))) == "available"


@pytest.mark.parametrize("features", [(), ("text.join",)], ids=["no-features", "other-feature"])
def test_a_run_whose_language_features_are_not_listed_is_unsupported(monkeypatch, features):
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", features)
    current = run(envelope(GREETING))
    assert policy.admission(current) == "unsupported"
    # Reads still withhold a run this platform cannot interpret.
    assert policy.unavailable(current)


def test_an_unsupported_run_is_available_again_after_an_upgrade(monkeypatch):
    current = run(envelope(GREETING))
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ())
    assert policy.admission(current) == "unsupported"
    monkeypatch.undo()
    assert policy.admission(current) == "available"


@pytest.mark.parametrize("features", [(), ("text.join",)], ids=["no-features", "other-feature"])
def test_reads_and_controls_of_an_unsupported_run_answer_ir_unsupported(monkeypatch, features):
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", features)
    with pytest.raises(CatalogError) as refused:
        policy.require_available(run(envelope(GREETING)))
    assert (refused.value.status, refused.value.code, refused.value.result) == (
        422,
        "WV-IR-UNSUPPORTED",
        {"reason": "ir_unsupported", "missing_features": ["text.concat"]},
    )


LEGACY = pytest.mark.parametrize(
    "current",
    [
        run(envelope({"literal": "plain"}), unavailable=True),
        run({}),
        run({**envelope({"literal": "plain"}), "digest": "sha256:" + "0" * 64}),
    ],
    ids=["unavailable-state", "malformed", "tampered"],
)


@LEGACY
def test_legacy_or_malformed_evidence_stays_unavailable(rolled_back, current):
    assert policy.admission(current) == "unavailable"


@LEGACY
def test_reads_and_controls_of_legacy_evidence_still_answer_legacy_unavailable(rolled_back, current):
    with pytest.raises(CatalogError) as refused:
        policy.require_available(current)
    assert (refused.value.status, refused.value.code) == (409, "WV-LEGACY-UNAVAILABLE")
    assert refused.value.result["value"] == {"id": str(current["id"]), "status": "waiting"}


async def test_scanners_skip_an_unsupported_run_without_a_policy_block(rolled_back, policy_blocks):
    runtime = RuntimeService(SimpleNamespace(outbox=None))
    tx = SimpleNamespace(scope=SCOPE)
    assert await runtime.observe_unavailable(tx, run(envelope(GREETING)))
    assert policy_blocks == []


async def test_scanners_still_block_legacy_evidence(rolled_back, policy_blocks):
    runtime = RuntimeService(SimpleNamespace(outbox=None))
    legacy = run(envelope({"literal": "plain"}), unavailable=True)
    assert await runtime.observe_unavailable(SimpleNamespace(scope=SCOPE), legacy)
    assert policy_blocks == [legacy["id"]]


async def test_settling_an_unsupported_run_leaves_its_deadlines_for_an_upgrade(rolled_back):
    class Untouched:
        tx = SimpleNamespace(session=SimpleNamespace(info={}))

        def __getattr__(self, name):
            raise AssertionError(f"settle used the repository ({name}) for a run it cannot interpret")

    assert await settle(Untouched(), run(envelope(GREETING))) == 0
