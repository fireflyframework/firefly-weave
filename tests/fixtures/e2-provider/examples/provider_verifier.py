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
"""Provider verifier design example, excluded from installed service discovery.

Implement the ProviderVerifier port when adding a real provider. Verify raw
bytes and configured provider identity before normalization. Event IDs must be
stable; cap the raw body at 1 MiB and a normalized batch at 100 events. Do not
infer Weave principals or scope from a sender, dispatch workflows here, perform
network calls in admission hooks, or treat this skeleton as a supported adapter.
"""

from typing import NoReturn


def verify_example() -> NoReturn:
    """Fail closed until a provider-specific signature and event contract exists."""
    raise NotImplementedError("Implement the provider verification port and fixture tests")
