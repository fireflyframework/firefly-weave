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

"""What workers and the operations runner print about a call that failed: never text a server or the network chose."""

import pytest

from firefly_weave.contracts.public import Problem
from firefly_weave.sdk._retry import kind
from firefly_weave.sdk.errors import WeaveError


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("WV-X\n2026-11-16 INFO forged", id="newline"),
        pytest.param("WV-X\x1b[2Jforged", id="escape-sequence"),
        pytest.param("WV-" + "A" * 62, id="longer-than-64"),
        pytest.param("WV-" + "A" * 1_000_000, id="one-megabyte"),
    ],
)
def test_a_code_the_platform_does_not_mint_is_never_printed(code):
    # Logs and terminals show the code as it is, so a newline or an escape sequence in it could forge a line.
    assert kind(WeaveError(Problem(status=503, code=code, message="Remote request failed"))) == "WV-HTTP (HTTP 503)"
