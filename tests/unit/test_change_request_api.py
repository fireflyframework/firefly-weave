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

"""The change request operations, their capabilities and their list filters."""

import pytest
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.deployments import (
    ChangeRequest,
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ChangeRequestQuery,
)
from firefly_weave.contracts.surface import ENVIRONMENT, OPERATIONS, Page

BASE = ENVIRONMENT + "/deployment-change-requests"
STATES = [
    "observing",
    "ready_to_plan",
    "awaiting_approval",
    "approved",
    "draining",
    "applying",
    "succeeded",
    "failed",
    "reconciliation_required",
    "expired",
    "declined",
    "cancelled",
    "superseded",
]


@pytest.mark.parametrize(
    ("identifier", "path", "method", "capability", "request_model", "response", "statuses", "flags"),
    [
        ("create", BASE, "POST", "deployment.plan", ChangeRequestCreate, ChangeRequest, (201,), {"idempotency"}),
        ("list", BASE, "GET", "deployment.read", None, Page[ChangeRequest], (200,), {"page"}),
        ("read", BASE + "/{identifier}", "GET", "deployment.read", None, ChangeRequest, (200,), set()),
        (
            "advance",
            BASE + "/{identifier}/advance",
            "POST",
            "deployment.plan",
            None,
            ChangeRequest,
            (200,),
            {"revision", "etag"},
        ),
        (
            "approve",
            BASE + "/{identifier}/approve",
            "POST",
            "deployment.approve",
            ChangeRequestApprove,
            ChangeRequest,
            (200,),
            set(),
        ),
        (
            "replan",
            BASE + "/{identifier}/replan",
            "POST",
            "deployment.plan",
            None,
            ChangeRequest,
            (200,),
            {"revision", "etag"},
        ),
        (
            "decline",
            BASE + "/{identifier}/decline",
            "POST",
            "deployment.approve",
            ChangeRequestDecision,
            ChangeRequest,
            (200,),
            set(),
        ),
        (
            "cancel",
            BASE + "/{identifier}/cancel",
            "POST",
            "deployment.plan",
            ChangeRequestDecision,
            ChangeRequest,
            (200,),
            set(),
        ),
    ],
)
def test_each_change_request_operation_has_its_path_capability_and_flags(
    identifier, path, method, capability, request_model, response, statuses, flags
):
    operation = OPERATIONS["deployment_change_requests." + identifier]
    assert (operation.path, operation.method, operation.capability) == (path, method, capability)
    assert (operation.request, operation.response, operation.statuses) == (request_model, response, statuses)
    assert operation.idempotency == ("idempotency" in flags)
    assert operation.page == ("page" in flags)
    assert operation.revision == ("required" if "revision" in flags else "none")
    assert operation.etag == ("etag" in flags)
    assert operation.query == (ChangeRequestQuery if identifier == "list" else None)


def test_the_family_has_exactly_its_operations():
    assert sorted(name for name in OPERATIONS if name.startswith("deployment_change_requests.")) == sorted(
        "deployment_change_requests." + verb
        for verb in ("create", "list", "read", "advance", "approve", "replan", "decline", "cancel")
    )


def test_the_list_query_filters_by_target_states_and_requester():
    query = parse_query(
        QueryParams([("state", "awaiting_approval"), ("state", "expired"), ("requested_by", "me")]),
        ChangeRequestQuery,
    )
    assert query.state == ["awaiting_approval", "expired"] and query.requested_by == "me"
    assert ChangeRequestQuery(state=STATES).state == STATES
    with pytest.raises(ValidationError):
        ChangeRequestQuery.model_validate({"state": [*STATES, "observing"]})
    with pytest.raises(ValidationError):
        parse_query(QueryParams([("requested_by", "someone")]), ChangeRequestQuery)
    with pytest.raises(ValidationError):
        parse_query(QueryParams([("state", "unknown")]), ChangeRequestQuery)
    with pytest.raises(ValueError, match="Unknown query parameter"):
        parse_query(QueryParams([("planned_by", "me")]), ChangeRequestQuery)


def test_list_cursors_are_bound_to_the_filters():
    assert ChangeRequestQuery().cursor_collection() == ChangeRequestQuery(limit=10).cursor_collection()
    assert ChangeRequestQuery().cursor_collection() != ChangeRequestQuery(requested_by="me").cursor_collection()


def test_the_studio_bridge_admits_change_requests():
    from firefly_weave.studio.service import STUDIO_FAMILIES

    assert "deployment_change_requests" in STUDIO_FAMILIES
