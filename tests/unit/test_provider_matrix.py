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
"""Exercise recovery observations without starting installed processes or services."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

spec = importlib.util.spec_from_file_location(
    "provider_matrix_regression", Path(__file__).parents[1] / "e2e/support/provider_matrix.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def observed(dispatched):
    return {
        "receipts": [
            {
                "id": str(i),
                "state": "dispatched" if i in dispatched else "pending",
                "run_id": f"run-{i}" if i in dispatched else None,
            }
            for i in range(6)
        ],
        "events": [{"run_id": f"run-{i}", "id": f"event-{i}", "type": "started", "sequence": 1} for i in dispatched],
        "teams": [],
        "wa_facts": [{"id": str(i)} for i in range(5)],
        "wa_states": [{"progress": 3, "failed_seen": True, "deleted_seen": True, "fact_count": 5}],
    }


@pytest.mark.parametrize("kind", ["dispatch", "admission"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("invalid", [None, "shared-run", "duplicate-start", "missing-start"])
async def test_batch_dispatch_correlates_barrier_and_each_final_run(tmp_path, monkeypatch, kind, after, invalid):
    # Other receipts can commit before the explicitly selected receipt reaches its barrier.
    committed = observed({1, 2, 3} | ({0} if after else set()))
    final = observed(set(range(6)))
    if invalid == "shared-run":
        final["receipts"][5]["run_id"] = "run-4"
    elif invalid == "duplicate-start":
        final["events"][5]["run_id"] = "run-4"
    elif invalid == "missing-start":
        final["events"].pop()
    if kind == "dispatch":
        snapshots = iter([observed(set()), committed, committed, final])
    else:
        committed = observed(set())
        if not after:
            committed.update(receipts=[], wa_facts=[], wa_states=[])
        snapshots = iter([committed, observed(set()), final])

    async def rows(*_):
        return next(snapshots)

    monkeypatch.setattr(module, "rows", rows)
    monkeypatch.setattr(module, "prepare", AsyncMock(return_value=({"id": "source"}, {}, lambda _: {}, None)))
    monkeypatch.setattr(module, "set_scheduler", AsyncMock())
    trial = SimpleNamespace(
        client=SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200))),
        api_env={},
        scope=SimpleNamespace(model_dump=lambda **_: {}),
        replace_api=AsyncMock(),
        kill_at_barrier=AsyncMock(return_value={}),
        kill_selected=AsyncMock(),
        directory=tmp_path,
        artifact_sha="wheel",
    )
    expected = []

    def invariants(*_, expected_event_types):
        expected.append(expected_event_types)
        return {"lost_committed_work": 0}

    matrix = SimpleNamespace(invariant_counts=invariants, private_json=lambda *_: None)
    boundary = ("after" if after else "before") + "_provider_" + kind + "_commit"
    if invalid:
        with pytest.raises(AssertionError):
            await module.exercise(trial, "whatsapp", boundary, matrix)
    else:
        result = await module.exercise(trial, "whatsapp", boundary, matrix)
        assert result["final"] == final
        assert expected == [{"started": 6}]


async def test_preparing_second_provider_preserves_first_package_and_secret(tmp_path, monkeypatch):
    from uuid import uuid4

    from firefly_weave.contracts.access import Scope

    monkeypatch.setenv("WEAVE_E2E_TEAMS_PYTHON", "installed-teams-python")
    monkeypatch.setattr(module, "set_scheduler", AsyncMock())
    posted = []

    async def post(_, body):
        posted.append(body)
        return {"id": str(uuid4())}

    trial = SimpleNamespace(
        api_env={"WEAVE_CONNECTOR_PACKAGES": '["existing-package"]', "WEAVE_SECRET_GRANTS": "[]"},
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        directory=tmp_path,
        prepare_workflow=AsyncMock(return_value=({"id": str(uuid4())}, None, None)),
        host=SimpleNamespace(publish=AsyncMock(return_value={"id": str(uuid4())}), post=post, environment_url="env"),
    )
    fixture = SimpleNamespace(
        signed_activity=SimpleNamespace(__wrapped__=lambda: ({}, {}, lambda: "token", "{}")),
        body=lambda *_, **__: {},
    )
    matrix = SimpleNamespace(ROOT=tmp_path, load=lambda *_: fixture, private_json=lambda *_: None)
    await module.prepare(trial, "teams", matrix)
    first_grant = json.loads(trial.api_env["WEAVE_SECRET_GRANTS"])[0]
    first_secret = trial.api_env[first_grant["locator"]]
    await module.prepare(trial, "whatsapp", matrix)
    assert json.loads(trial.api_env["WEAVE_CONNECTOR_PACKAGES"]) == [
        "existing-package",
        "firefly-weave:weave-teams:firefly_weave.connectors.teams:package",
        "firefly-weave:weave-whatsapp:firefly_weave.connectors.whatsapp:package",
    ]
    grants = json.loads(trial.api_env["WEAVE_SECRET_GRANTS"])
    assert len({grant["locator"] for grant in grants}) == 4
    assert len({grant["handle"] for grant in grants}) == 4
    assert trial.api_env[first_grant["locator"]] == first_secret
    references = [body["secretRef"] for body in posted if "secretRef" in body]
    assert set(references[0].values()).isdisjoint(references[1].values())
    assert set(references[0].values()) | set(references[1].values()) == {g["handle"] for g in grants}
