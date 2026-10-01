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
"""Deterministic bounded batch classification with no partial admission effects."""

from firefly_weave.contracts.providers import ProviderEvent
from firefly_weave.definitions.models import CatalogError


def classify(events: tuple[ProviderEvent, ...], prior: dict[tuple[str, str], str]) -> tuple[ProviderEvent, ...]:
    if len(events) > 100:
        raise CatalogError(413, "WV-PROVIDER-BATCH", "Provider batch exceeds limit")
    seen = dict(prior)
    new = []
    for event in events:
        identity = event.kind, event.event_id
        if identity in seen:
            if seen[identity] != event.fingerprint:
                raise CatalogError(409, "WV-PROVIDER-CONFLICT", "Provider event identity conflict")
        else:
            seen[identity] = event.fingerprint
            new.append(event)
    return tuple(new)
