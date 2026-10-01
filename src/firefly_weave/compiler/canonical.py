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

"""RFC 8785 executable identity, independent of author formatting."""

import hashlib

import rfc8785

from firefly_weave.contracts.values import JsonObject


def canonical_bytes(value: JsonObject) -> bytes:
    return rfc8785.dumps(value)


def canonical_digest(value: JsonObject) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
