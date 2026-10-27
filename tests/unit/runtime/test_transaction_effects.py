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

"""Whether a failed participant may have applied anything: each attempt judged by its release, folded over attempts."""

import pytest

from firefly_weave.runtime.transactions import failure_effect, fold

ADD = {"sideEffect": "non_idempotent", "definiteFailures": ["VALIDATION"]}
CONNECTOR = {
    "implementation": {"kind": "connector", "uses": "target-system@1.0.0", "action": "add"},
    "sideEffect": "idempotency_key",
    "transactionAware": True,
}
# A worker action whose definition claims awareness: only the claiming release's own declaration counts.
WORKER = {
    "implementation": {"kind": "worker", "taskType": "add-quantity", "taskVersion": "1.0.0"},
    "sideEffect": "non_idempotent",
    "transactionAware": True,
}


def attempt(action, error, aware=False):
    return failure_effect(action, error, transaction_aware=aware)


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        # A temporary code, then a definite failure from an aware release: the first may have applied.
        (
            [attempt(ADD, {"code": "TIMEOUT"}), attempt(ADD, {"code": "REFUSED", "effect": "none"}, aware=True)],
            "unknown",
        ),
        ([attempt(ADD, {"code": "REFUSED", "effect": "none"}, aware=True)], "none"),
        ([attempt(ADD, {"code": "REFUSED", "effect": "none"})], "unknown"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "failed"})], "unknown"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "not_started"}, aware=True)], "none"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "not_started"})], "unknown"),
        ([attempt(ADD, {"code": "VALIDATION"})], "none"),
        # A lease that expired in an earlier attempt, then a definite failure.
        (["unknown", attempt(ADD, {"code": "VALIDATION"})], "unknown"),
        # Revoked before any worker claimed it.
        (["none"], "none"),
        # A connector release that declares itself aware in its action specs.
        ([attempt(CONNECTOR, {"code": "REFUSED", "effect": "none"})], "none"),
        # A worker action marked aware, from a release that declared itself unaware (or never declared).
        ([attempt(WORKER, {"code": "REFUSED", "effect": "none"})], "unknown"),
        ([attempt(WORKER, {"code": "REFUSED", "outcome": "not_started"})], "unknown"),
        # The same action from a release that declared itself aware.
        ([attempt(WORKER, {"code": "REFUSED", "effect": "none"}, aware=True)], "none"),
    ],
)
def test_a_participant_applied_nothing_only_when_every_attempt_says_so_from_a_release_that_may(attempts, expected):
    effect = "none"
    for observed in attempts:
        effect = fold(effect, observed)
    assert effect == expected


def test_once_unknown_always_unknown():
    assert fold("unknown", "none") == "unknown" and fold("none", "none") == "none" and fold("none", None) == "unknown"
