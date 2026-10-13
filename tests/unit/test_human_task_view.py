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

"""A human task names its step, and inside loops also its instance key and iteration."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.human_tasks import HumanTask
from firefly_weave.human_tasks.service import task_view


def row(node_id: str) -> dict:
    return {
        "tenant_id": uuid4(),
        "id": uuid4(),
        "run_id": uuid4(),
        "node_id": node_id,
        "revision": 1,
        "status": "ready",
        "title": "Review the invoice",
        "context": {},
        "form_schema": {},
        "decisions": ["approve"],
        "assignment": {"binding_id": str(uuid4()), "revision": 1},
        "created_at": datetime(2026, 10, 13, tzinfo=UTC),
    }


def test_a_task_outside_loops_keeps_its_exact_view():
    view = task_view(row("review")).model_dump(mode="json")
    assert view["node_id"] == "review"
    assert "instance_key" not in view and "iteration" not in view


@pytest.mark.parametrize(("key", "iteration"), [("review[3]", [3]), ("review[3][0]", [3, 0])])
def test_a_task_inside_loops_names_its_step_key_and_iteration(key, iteration):
    view = task_view(row(key))
    assert (view.node_id, view.instance_key, view.iteration) == ("review", key, iteration)
    assert view.model_dump(mode="json")["iteration"] == iteration


@pytest.mark.parametrize(
    ("node_id", "instance_key", "iteration"),
    [("review", "review", []), ("review", "approve[1]", [1]), ("review", "review[1]", []), ("review", "", [1])],
)
def test_a_task_view_refuses_an_identity_of_another_step(node_id, instance_key, iteration):
    with pytest.raises(ValidationError):
        HumanTask.model_validate(
            {
                **task_view(row("review")).model_dump(),
                "node_id": node_id,
                "instance_key": instance_key,
                "iteration": iteration,
            }
        )
