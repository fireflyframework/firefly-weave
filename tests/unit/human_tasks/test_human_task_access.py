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

"""Who may see and answer a human task: a distinct review shuts its run's starter out, managers included."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.human_tasks import AssignmentPin
from firefly_weave.definitions.models import CatalogError
from firefly_weave.human_tasks.service import HumanTaskService, eligible, review_digest, task_view

STARTER = uuid4()
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
REVIEW = "support#1.1.review"


def pin(**fields) -> AssignmentPin:
    return AssignmentPin(binding_id=uuid4(), revision=1, **fields)


def row(node_id: str = REVIEW, *, context: dict | None = None, assignment: AssignmentPin | None = None) -> dict:
    return {
        "tenant_id": uuid4(),
        "id": uuid4(),
        "run_id": uuid4(),
        "node_id": node_id,
        "revision": 1,
        "status": "ready",
        "title": "Review cancel_order for support",
        "context": context or {},
        "form_schema": {},
        "decisions": ["approve", "reject"],
        "assignment": (assignment or pin()).model_dump(mode="json"),
        "created_at": datetime(2026, 11, 12, tzinfo=UTC),
    }


class Granting:
    """Authorization that grants every capability, as it does for a person who manages human tasks."""

    def require(self, *args, **kwargs) -> None:
        return None


async def manager_reads(task: dict) -> str:
    """What a person who manages human tasks gets when they read ``task``: the task, or the refusal's code."""
    tasks = HumanTaskService(SimpleNamespace(definitions=SimpleNamespace(authorization=Granting())))
    try:
        # The excluded check comes first, so no assignment query runs and no repository is needed.
        await tasks._readable(None, task, Principal(id=STARTER, kind="human"), SCOPE, AuditContext())
    except CatalogError as error:
        return f"{error.status} {error.code}"
    return "readable"


async def test_an_excluded_principal_is_refused_even_when_assigned():
    with pytest.raises(CatalogError) as error:
        await eligible(None, pin(principal_ids=[STARTER], excluded_principal_ids=[STARTER]), STARTER)

    assert (error.value.status, error.value.code) == (403, "WV-REVIEW-DISTINCT")


@pytest.mark.parametrize(
    ("excluded", "expected"),
    [(STARTER, "403 WV-REVIEW-DISTINCT"), (uuid4(), "readable")],
    ids=["started-the-run", "someone-else-started-it"],
)
async def test_a_manager_who_started_the_run_cant_read_its_distinct_review(excluded, expected):
    assert await manager_reads(row(assignment=pin(excluded_principal_ids=[excluded]))) == expected


def test_a_reviews_decision_carries_the_digest_stored_with_its_task():
    task = row(context={"inputDigest": "sha256:ab", "aiWritten": True})

    assert review_digest(task) == {"input_digest": "sha256:ab"}


@pytest.mark.parametrize("node_id", ["approve", "support#1.1", "support[2]#1"], ids=["human-step", "tool", "turn"])
def test_a_task_that_is_not_a_review_keeps_its_event_bytes(node_id):
    # An author's own task context may hold any key; only a review's digest is the server's.
    assert review_digest(row(node_id, context={"inputDigest": "sha256:ab"})) == {}


def test_the_task_view_never_names_who_is_excluded():
    view = task_view(row(assignment=pin(excluded_principal_ids=[STARTER])))

    assert "excluded_principal_ids" not in view.model_dump(mode="json")["assignment"]
