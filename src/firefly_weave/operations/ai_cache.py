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

"""Process-local, expiring model metadata with explicit entry and byte ownership."""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.ai import AIModelsResult
from firefly_weave.definitions.models import CatalogError

type CacheKey = tuple[UUID, UUID, UUID, UUID, str]


@dataclass(frozen=True)
class CacheRecord:
    result: AIModelsResult
    expires: float
    size: int


class AIModelCache:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 128,
        max_bytes: int = 8 * 1024 * 1024,
        ttl: float = 300.0,
    ) -> None:
        self.clock, self.max_entries, self.max_bytes, self.ttl = clock, max_entries, max_bytes, ttl
        self._records: OrderedDict[CacheKey, CacheRecord] = OrderedDict()
        self._bytes = 0

    def _remove(self, key: CacheKey) -> None:
        record = self._records.pop(key, None)
        if record is not None:
            self._bytes -= record.size

    def _expire(self) -> None:
        now = self.clock()
        for key, record in list(self._records.items()):
            if record.expires <= now:
                self._remove(key)

    def get(self, key: CacheKey) -> AIModelsResult | None:
        self._expire()
        record = self._records.get(key)
        if record is None:
            return None
        self._records.move_to_end(key)
        return record.result.model_copy(deep=True)

    def put(self, key: CacheKey, result: AIModelsResult) -> None:
        payload_bytes = len(result.model_dump_json().encode("utf-8"))
        if payload_bytes > 262144:
            raise CatalogError(429, "WV-AI-LIMIT", "The model metadata is too large")
        self._remove(key)
        self._expire()
        while self._records and (
            len(self._records) >= self.max_entries or self._bytes + payload_bytes > self.max_bytes
        ):
            self._remove(next(iter(self._records)))
        if payload_bytes > self.max_bytes or self.max_entries <= 0:
            return
        self._records[key] = CacheRecord(result.model_copy(deep=True), self.clock() + self.ttl, payload_bytes)
        self._bytes += payload_bytes

    def discard_revision(self, scope: Scope, revision_id: UUID) -> None:
        prefix = (scope.tenant_id, scope.project_id, scope.environment_id, revision_id)
        for key in list(self._records):
            if key[:4] == prefix:
                self._remove(key)
