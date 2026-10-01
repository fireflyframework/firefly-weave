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

"""Inventory facts are mandatory before a payload can establish readiness."""

from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.operations.compatibility import classify_inventory_row


def test_missing_or_false_database_fact_verification_is_incomplete():
    for facts in (None, False, 1, "true"):
        assert classify_inventory_row("worker", {}, facts, ConnectorRegistry()) == "inventory_incomplete"


def test_validated_facts_do_not_bypass_payload_classification():
    assert classify_inventory_row("worker", {}, True, ConnectorRegistry()) == "worker_protocol_unsupported"


def test_omitted_finding_still_changes_inventory_admission():
    from firefly_weave.contracts.compatibility import CompatibilityFinding
    from firefly_weave.operations.compatibility import FindingAccumulator

    for code in ("ir_unsupported", "inventory_incomplete"):
        findings = FindingAccumulator()
        for _ in range(1000):
            findings.record(CompatibilityFinding(kind="run", code="legacy_policy_blocked"))
        findings.record(CompatibilityFinding(kind="run", code=code))
        assert len(findings.items) == 1000 and findings.truncated
        assert findings.blocking
        assert findings.incomplete == (code == "inventory_incomplete")
