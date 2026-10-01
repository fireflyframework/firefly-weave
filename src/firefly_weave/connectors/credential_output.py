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
"""Credential matching over already bounded, validated JSON output."""

import json
from collections.abc import Callable

import rfc8785

from firefly_weave.contracts.values import JsonValue


def contains_sensitive(value: JsonValue, matches: Callable[[str], bool]) -> bool:
    if isinstance(value, str):
        return matches(value)
    if isinstance(value, dict):
        return any(matches(key) or contains_sensitive(child, matches) for key, child in value.items())
    if isinstance(value, list):
        return any(contains_sensitive(child, matches) for child in value)
    # JSON scalars can carry the same credential without string delimiters. Check
    # both the canonical and ordinary JSON number spelling (e.g. 1 and 1.0).
    return matches(rfc8785.dumps(value).decode()) or matches(json.dumps(value, allow_nan=False))
