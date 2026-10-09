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

"""Bounded exact run discovery and filter-bound cursors."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.api.transport import decode_cursor, encode_cursor
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.runtime import RunListFilters


def test_filter_cursor_rejects_changed_business_context_and_archive_selection():
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    filters = RunListFilters(business_key="費" * 200, correlation_key="thread", status="waiting")
    identifier = uuid4()
    cursor = encode_cursor(scope, filters.cursor_collection(), identifier)
    assert len(cursor) <= 1024
    assert decode_cursor(cursor, scope, filters.cursor_collection()) == identifier
    for changed in (
        {"business_key": "other"},
        {"correlation_key": "other"},
        {"status": "succeeded"},
        {"include_archived": True},
    ):
        with pytest.raises(ValueError):
            decode_cursor(cursor, scope, filters.model_copy(update=changed).cursor_collection())


@pytest.mark.parametrize("value", [{"status": "invalid"}, {"business_key": "x" * 201}, {"correlation_key": "x" * 201}])
def test_filters_reject_invalid_status_and_unbounded_keys(value):
    with pytest.raises(ValidationError):
        RunListFilters.model_validate(value)


def test_explicit_id_queries_retain_exact_legacy_cursor_collections():
    from firefly_weave.contracts.run_views import RunListQuery

    old = RunListFilters(business_key="invoice", correlation_key="thread", status="waiting", include_archived=True)
    query = RunListQuery(
        order="id", business_key="invoice", correlation_key="thread", status=["waiting"], include_archived=True
    )
    assert query.cursor_collection() == old.cursor_collection()
    assert query.model_copy(update={"include_test": True}).cursor_collection() != old.cursor_collection()
    assert query.model_copy(update={"status": ["waiting", "failed"]}).cursor_collection() != old.cursor_collection()
    assert query.model_copy(update={"order": "started_desc"}).cursor_collection() != old.cursor_collection()
