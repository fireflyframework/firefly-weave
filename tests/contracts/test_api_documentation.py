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

"""Keep the public API inventory aligned with the operation authority registry."""

import re
from pathlib import Path

from firefly_weave.contracts.surface import OPERATIONS


def test_public_api_inventory_matches_current_operations():
    document = Path(__file__).resolve().parents[2] / "docs/reference/api.md"
    rows = re.findall(r"^\| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$", document.read_text(), re.M)
    assert len(rows) == len({row[0] for row in rows}), "Duplicate API operation rows"
    documented = {identifier: (route, authority) for identifier, route, authority in rows}
    expected = {
        operation.id: (
            f"{operation.method} {operation.canonical_path}",
            operation.capability or "Public probe",
        )
        for operation in OPERATIONS.values()
    }
    assert documented == expected
