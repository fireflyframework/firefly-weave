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

"""Confirm it had no effect is a resolution kind of its own; it never carries an output."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.operations import IncidentResolution, RecordedResolution


def test_confirming_no_effect_is_a_resolution_kind_and_any_other_new_kind_is_refused():
    recorded = RecordedResolution(
        receipt_id=uuid4(), kind="accept_reconciled_failure", reason="checked", evidence_reference="ticket 42"
    )
    assert recorded.model_dump(mode="json")["output"] is None
    request = IncidentResolution(receipt_id=uuid4(), kind="accept_reconciled_failure", reason="checked")
    assert "output" not in request.model_fields_set
    with pytest.raises(ValidationError):
        RecordedResolution(receipt_id=uuid4(), kind="accept_reconciled_maybe", reason="checked")
