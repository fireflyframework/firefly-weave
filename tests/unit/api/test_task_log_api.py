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

"""`tasks.log` reads at most 256 KiB, writes outside the project's operation lock, and answers 202."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.api.workers import WorkerController
from firefly_weave.contracts.workers import TaskLogAccepted
from firefly_weave.definitions.models import CatalogError

PATH = {"tenant": str(uuid4()), "project": str(uuid4()), "environment": str(uuid4())}


def request(body: bytes) -> Request:
    chunks = [body[index : index + 65536] for index in range(0, len(body), 65536)] or [b""]

    async def receive():
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/tasks/logs",
        "headers": [],
        "path_params": PATH,
        "state": {"principal": SimpleNamespace(id=uuid4()), "audit_context": AuditContext()},
    }
    return Request(scope, receive)


class Recorded:
    def __init__(self) -> None:
        self.calls = []
        self.opened = []

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        self.opened.append((mutation, admission))
        yield SimpleNamespace(scope=scope)

    async def log(self, tx, body, **authority):
        self.calls.append((body, authority))
        return TaskLogAccepted(accepted=len(body.entries), dropped=0)


def controller(recorded: Recorded) -> WorkerController:
    service = SimpleNamespace(definitions=SimpleNamespace(uow=recorded))
    settings = SimpleNamespace(execution_log_daily_entries=4321)
    return WorkerController(service, recorded, settings)  # type: ignore[arg-type]


def body(message: str = "Checked the order") -> bytes:
    lease = {"task_id": str(uuid4()), "generation": 1, "owner": str(uuid4()), "token": "opaque"}
    entry = {"at": datetime.now(UTC).isoformat(), "source": "worker", "level": "info", "message": message}
    return json.dumps({"lease": lease, "entries": [entry]}).encode()


async def test_a_log_request_is_accepted_with_202_outside_the_operation_lock():
    recorded = Recorded()
    response = await controller(recorded).log(request(body()))
    assert response.status_code == 202
    assert json.loads(response.body) == {"accepted": 1, "dropped": 0}
    assert recorded.opened == [(True, False)]
    ((sent, authority),) = recorded.calls
    assert sent.entries[0].message == "Checked the order"
    assert authority["daily_limit"] == 4321


async def test_a_log_request_over_256_kib_is_refused_before_anything_is_written():
    recorded = Recorded()
    oversized = body() + b" " * (256 * 1024)
    with pytest.raises(CatalogError) as refused:
        await controller(recorded).log(request(oversized))
    assert (refused.value.status, refused.value.code) == (413, "WV-LOG-SIZE")
    assert recorded.calls == [] and recorded.opened == []
