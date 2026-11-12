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

"""For any pattern of failures, each setting keeps its promise: stop changes nothing, continue never stops the run,
an error path runs instead of the step's success, and an effect that may have happened is never handled."""

from functools import cache

from hypothesis import given, settings
from hypothesis import strategies as st
from step_error_support import Run, action, compiled, loop, path, transform

from firefly_weave.compiler.canonical import canonical_digest

ITEMS = st.lists(
    st.one_of(st.just({}), st.builds(lambda name: {"name": name}, st.text("abc", min_size=1, max_size=3))), max_size=8
)


@cache
def flow(setting: str, concurrency: int):
    extra = {} if setting == "omitted" else {"onError": setting}
    if setting == "errorOutput":
        extra["errorPath"] = path(output={"ref": "/steps/pick/error/instanceKey"})
    output = (
        {"op": {"name": "coalesce", "args": [{"ref": "/steps/pick/output"}, {"ref": "/steps/pick/error/instanceKey"}]}}
        if setting == "continue"
        else {"ref": "/steps/pick/output"}
    )
    return compiled(
        loop(
            "each",
            {"ref": "/input"},
            transform("pick", {"ref": "/item/name"}, **extra),
            output=output,
            concurrency=concurrency,
        ),
        output={"ref": "/steps/each/output"},
    )


def digests(setting: str, items: list, concurrency: int) -> list[str]:
    run = Run(flow(setting, concurrency), items)
    return [canonical_digest(run.start().model_dump(mode="json"))]


@settings(max_examples=60, deadline=None)
@given(ITEMS, st.integers(1, 3))
def test_an_explicit_stop_reduces_exactly_like_an_omitted_setting(items, concurrency):
    assert digests("stop", items, concurrency) == digests("omitted", items, concurrency)


@settings(max_examples=60, deadline=None)
@given(ITEMS, st.integers(1, 3))
def test_continue_never_stops_the_run_and_keeps_one_result_per_element(items, concurrency):
    run = Run(flow("continue", concurrency), items)
    result = run.start()
    assert run.state.status == "succeeded" and run.state.incidents == {}
    expected = [item.get("name", f"pick[{index}]") for index, item in enumerate(items)]
    assert run.state.output == expected
    failed = [change for change in result.steps if change.status == "failed"]
    assert [change.node_id for change in failed] == [f"pick[{i}]" for i, item in enumerate(items) if not item]
    assert all(change.output is None and change.error["instanceKey"] == change.node_id for change in failed)


@settings(max_examples=60, deadline=None)
@given(ITEMS, st.integers(1, 3))
def test_an_error_path_runs_instead_of_the_success_and_leaves_nothing_behind(items, concurrency):
    run = Run(flow("errorOutput", concurrency), items)
    result = run.start()
    assert run.state.output == [item.get("name", f"pick[{index}]") for index, item in enumerate(items)]
    spawned = [
        command.node_id
        for command in result.commands
        if command.kind == "spawn_branch" and command.branch == "errorPath"
    ]
    assert spawned == [f"pick[{i}]" for i, item in enumerate(items) if not item]
    assert run.state.branches == {} and run.state.joins == {}


@settings(max_examples=40, deadline=None)
@given(
    st.sampled_from(["TIMEOUT", "UNAVAILABLE", "REQUEST_REFUSED", "WV-TASK-LEASE-EXPIRED"]),
    st.sampled_from(["continue", "errorOutput"]),
)
def test_an_unknown_outcome_of_a_step_that_changes_data_is_never_handled(code, setting):
    extra = {"onError": setting} | ({"errorPath": path()} if setting == "errorOutput" else {})
    run = Run(compiled(action("place", uses="submit", value={"object": {}}, **extra)), {})
    run.start()
    run.fail("place", code, outcome="unknown")
    assert (run.state.status, run.state.incident, run.state.active) == ("suspended", "WV-RUNTIME-EVENT", ["place"])


@settings(max_examples=40, deadline=None)
@given(ITEMS, st.sampled_from(["continue", "errorOutput"]), st.integers(1, 3))
def test_reducing_the_same_history_again_reproduces_every_transition(items, setting, concurrency):
    assert digests(setting, items, concurrency) == digests(setting, items, concurrency)
