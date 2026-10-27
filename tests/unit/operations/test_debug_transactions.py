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

"""Debug sessions don't simulate transactions yet: they stop before the workflow starts and say how to run it."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from transaction_support import INPUT, add, compiled, hold, submit, transaction  # noqa: E402

from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402


def test_a_session_whose_workflow_has_a_transaction_stops_with_a_diagnostic():
    flow = compiled([transaction([hold(), add(), submit()])])
    mocks = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}
    view = Simulator(
        flow, mocks=mocks, input=dict(INPUT), now=datetime(2026, 10, 26, tzinfo=UTC)
    ).continue_until_breakpoint()
    [diagnostic] = view.diagnostics
    assert diagnostic.code == "WV-DEBUG-MISSING_MOCK"
    assert diagnostic.message == "A transaction can't be simulated yet; run it on a test activation."
    assert view.events == []
