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

"""Which transitions are settlement work, and so spend the run's control reserve instead of waiting for quota."""

from transaction_support import Run, add, compiled, hold, submit, transaction

from firefly_weave.runtime.settlement import spends_reserve


def flow() -> Run:
    run = Run(compiled([transaction([hold(), add(), add("add2"), submit()])], timeout=300))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    return run


def last(run: Run):
    return run.events[-1], run.transitions[-1]


def test_forward_work_waits_for_quota():
    run = flow()
    assert not spends_reserve(*last(run))
    run.done("add", {"id": "a-1"})
    assert not spends_reserve(*last(run))


def test_a_cancel_that_undoes_first_and_the_undo_results_spend_the_reserve():
    run = flow()
    run.send("cancelled", reason="stop")
    assert spends_reserve(*last(run))
    # add was in flight while the transaction went back: its result is settlement work.
    run.done("add", {"id": "a-1"})
    assert spends_reserve(*last(run))
    run.done("add#undo", {})
    assert spends_reserve(*last(run))


def test_a_transaction_deadline_spends_the_reserve_and_a_stop_is_already_terminal_control():
    run = flow()
    run.fire("move#hold")
    assert spends_reserve(*last(run))
    run = flow()
    run.send("cancelled", reason="stop", skip_undo=True)
    assert run.state.status == "cancelled" and not spends_reserve(*last(run))
