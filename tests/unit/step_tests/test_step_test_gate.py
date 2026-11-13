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
"""A step that changes data runs for real only when the person acknowledged exactly that effect."""

import pytest

from firefly_weave.step_tests.admission import confirmation_needed

EFFECTS = ("read_only", "idempotent", "idempotency_key", "non_idempotent")


@pytest.mark.parametrize("effect", ["read_only", "idempotent"])
@pytest.mark.parametrize("acknowledged", [None, *EFFECTS])
def test_reading_and_repeatable_steps_run_without_asking(effect, acknowledged):
    assert confirmation_needed(effect, acknowledged) is False


@pytest.mark.parametrize("effect", ["idempotency_key", "non_idempotent"])
@pytest.mark.parametrize("acknowledged", [None, "read_only", "idempotent", "idempotency_key", "non_idempotent"])
def test_steps_that_change_data_run_only_when_acknowledged_exactly(effect, acknowledged):
    assert confirmation_needed(effect, acknowledged) is (acknowledged != effect)
