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

"""A worker release's declaration is written once, by its first declaring claim, and only read after that."""

from typing import Any
from uuid import uuid4

import pytest

from firefly_weave.contracts.workers import ClaimWorker
from firefly_weave.definitions.models import CatalogError
from firefly_weave.workers.leases import release_awareness


class Declarations:
    """The declarations table of one environment, recording every write a claim makes."""

    def __init__(self, declared: bool | None = None) -> None:
        self.declared, self.writes = declared, 0

    async def execute(self, sql: str, **values: Any) -> None:
        assert sql.startswith("INSERT INTO worker_release_awareness")
        self.writes += 1
        if self.declared is None:
            self.declared = values["aware"]

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        return [] if self.declared is None else [{"transaction_aware": self.declared}]


async def test_the_first_declaring_claim_writes_the_declaration_and_later_polls_only_read_it():
    release = uuid4()
    table = Declarations()
    assert await release_awareness(table, release, ClaimWorker(release=release, transaction_aware=True)) is True
    assert table.writes == 1
    for _ in range(3):
        assert await release_awareness(table, release, ClaimWorker(release=release, transaction_aware=True)) is True
    assert await release_awareness(table, release, None) is True
    assert table.writes == 1


async def test_a_release_cannot_change_its_answer_or_speak_for_another():
    release = uuid4()
    table = Declarations(declared=False)
    with pytest.raises(CatalogError) as changed:
        await release_awareness(table, release, ClaimWorker(release=release, transaction_aware=True))
    with pytest.raises(CatalogError) as other:
        await release_awareness(table, release, ClaimWorker(release=uuid4(), transaction_aware=False))
    assert (changed.value.code, other.value.code) == ("WV-WORKER-RELEASE", "WV-WORKER-RELEASE")
    assert table.writes == 0
