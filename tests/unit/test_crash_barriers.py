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

"""Private socket handshakes make kill boundaries deterministic and bounded."""

import asyncio
import importlib.util
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest

MODULE_PATH = Path(__file__).parents[1] / "e2e/support/barriers.py"
spec = importlib.util.spec_from_file_location("weave_crash_barriers_test", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def test_pause_cannot_cross_boundary_without_correlated_release():
    parent, child = socket.socketpair()
    channel = module.BarrierChannel(child, str(uuid4()))
    parent.setblocking(False)
    reached = False

    async def transaction():
        nonlocal reached
        await channel.pause("before_run_commit", {"scope": str(uuid4())})
        reached = True

    task = asyncio.create_task(transaction())
    try:
        message = await module.read_message(parent, timeout=1)
        assert message["nonce"] == channel.nonce
        assert message["phase"] == "before_run_commit"
        assert message["pid"] == os.getpid()
        assert reached is False
        await module.release(parent, channel.nonce)
        await asyncio.wait_for(task, 1)
        assert reached
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        parent.close()
        channel.close()


@pytest.mark.parametrize("payload", [b"x" * 2049, b"{}\nextra", b'{"bad":NaN}\n'])
async def test_bad_or_oversized_frames_fail_closed(payload):
    parent, child = socket.socketpair()
    parent.setblocking(False)
    child.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_sendall(parent, payload)
        with pytest.raises(module.BarrierError):
            await module.read_message(child, timeout=0.1)
    finally:
        parent.close()
        child.close()


async def test_wrong_nonce_never_resumes_transaction():
    parent, child = socket.socketpair()
    parent.setblocking(False)
    channel = module.BarrierChannel(child, str(uuid4()))
    task = asyncio.create_task(channel.pause("after_run_commit", {}))
    try:
        await module.read_message(parent, timeout=1)
        await module.release(parent, str(uuid4()))
        with pytest.raises(module.BarrierError):
            await asyncio.wait_for(task, 1)
    finally:
        parent.close()
        channel.close()


async def test_dropped_parent_aborts_uncommitted_boundary():
    parent, child = socket.socketpair()
    parent.setblocking(False)
    channel = module.BarrierChannel(child, str(uuid4()))
    task = asyncio.create_task(channel.pause("before_completion_commit", {}))
    try:
        await module.read_message(parent, timeout=1)
        parent.close()
        with pytest.raises(module.BarrierError):
            await asyncio.wait_for(task, 1)
    finally:
        parent.close()
        channel.close()


@pytest.mark.parametrize("phase,expected", [("before_run_commit", []), ("after_run_commit", ["commit"])])
async def test_actual_owner_exit_is_the_commit_boundary(phase, expected):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    events = []
    parent, child = socket.socketpair()
    parent.setblocking(False)
    channel = module.BarrierChannel(child, str(uuid4()))
    target = module.CommitTarget(str(uuid4()), phase, "POST", "/runs", {"project_id": "project"})

    @asynccontextmanager
    async def owning(self, scope, **kwargs):
        try:
            yield object()
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    wrapped = module.instrument_commit(owning, target, channel)

    async def app(scope, receive, send):
        async with wrapped(None, SimpleNamespace(project_id="project")):
            pass

    asgi = module.CorrelatedRequest(app, target)
    task = asyncio.create_task(
        asgi(
            {
                "type": "http",
                "method": "POST",
                "path": "/runs",
                "headers": [(b"x-weave-crash-case", target.case.encode())],
            },
            None,
            None,
        )
    )
    try:
        await module.read_message(parent, timeout=1)
        assert events == expected
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert events == (["rollback"] if phase.startswith("before") else ["commit"])
    finally:
        parent.close()
        channel.close()


async def test_unrelated_scope_and_readonly_transactions_do_not_pause():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    parent, child = socket.socketpair()
    channel = module.BarrierChannel(child, str(uuid4()))
    target = module.CommitTarget(str(uuid4()), "after_run_commit", "POST", "/runs", {"project_id": "project"})

    @asynccontextmanager
    async def owning(self, scope, **kwargs):
        yield object()

    wrapped = module.instrument_commit(owning, target, channel)
    token = module._request_case.set(target.case)
    try:
        async with wrapped(None, SimpleNamespace(project_id="other")):
            pass
        async with wrapped(None, SimpleNamespace(project_id="project"), mutation=False):
            pass
        assert not channel.used
    finally:
        module._request_case.reset(token)
        parent.close()
        channel.close()


async def test_nearest_product_owner_excludes_nested_lookup_transaction():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    parent, child = socket.socketpair()
    parent.setblocking(False)
    channel = module.BarrierChannel(child, str(uuid4()))
    target = module.CommitTarget(
        str(uuid4()),
        "after_provider_admission_commit",
        "POST",
        "/provider-ingress/x",
        {"project_id": "project"},
        owner="firefly_weave.providers.fixture:receive",
    )
    committed = []

    @asynccontextmanager
    async def owning(self, scope):
        yield object()
        committed.append("commit")

    namespace = {
        "__name__": "firefly_weave.providers.fixture",
        "wrapped": module.instrument_commit(owning, target, channel),
        "scope": SimpleNamespace(project_id="project"),
    }
    exec(
        "async def lookup():\n    async with wrapped(None, scope):\n        pass\n"
        "async def receive():\n    await lookup()\n    async with wrapped(None, scope):\n        pass\n",
        namespace,
    )
    token = module._request_case.set(target.case)
    task = asyncio.create_task(namespace["receive"]())
    module._request_case.reset(token)
    try:
        message = await module.read_message(parent, timeout=1)
        assert message["phase"] == target.phase and committed == ["commit", "commit"]
        await module.release(parent, channel.nonce)
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        parent.close()
        channel.close()
