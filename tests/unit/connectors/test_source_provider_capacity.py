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
"""Real thread barriers prove provider capacity remains reserved after cancellation."""

import asyncio
import subprocess
import sys
import threading

import pytest

from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.contracts.connectors import ResolvedSecret


async def test_provider_timeout_cannot_release_running_thread_capacity():
    from firefly_weave.connections.source_bindings import resolve_secret

    barriers = [threading.Event() for _ in range(4)]
    entered = [threading.Event() for _ in range(4)]

    def resolve(index):
        entered[index].set()
        barriers[index].wait(5)
        return ResolvedSecret(value="opaque-test-secret")

    tasks = [asyncio.create_task(resolve_secret(lambda i=i: resolve(i))) for i in range(4)]
    try:
        for _ in range(100):
            if all(e.is_set() for e in entered):
                break
            await asyncio.sleep(0.01)
        assert all(e.is_set() for e in entered)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(SecretUnavailable):
            await resolve_secret(lambda: ResolvedSecret(value="must-not-start"))
    finally:
        for barrier in barriers:
            barrier.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    for _ in range(100):
        try:
            result = await resolve_secret(lambda: ResolvedSecret(value="recovered"))
            assert result.value == "recovered"
            break
        except SecretUnavailable:
            await asyncio.sleep(0.01)
    else:
        pytest.fail("Finished provider capacity was not recovered")


async def test_provider_thread_construction_failure_releases_reservation(monkeypatch):
    import firefly_weave.connections.source_bindings as module

    original = threading.Thread

    def failed(*args, **kwargs):
        raise RuntimeError("thread construction failed before starting")

    monkeypatch.setattr(threading, "Thread", failed)
    for _ in range(5):
        with pytest.raises(RuntimeError, match="thread construction failed"):
            await module.resolve_secret(lambda: ResolvedSecret(value="none"))
    monkeypatch.setattr(threading, "Thread", original)
    assert (await module.resolve_secret(lambda: ResolvedSecret(value="works"))).value == "works"


def test_rejected_thread_starts_never_execute_provider_work():
    # A fresh interpreter guarantees the old executor has no reusable worker.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import threading
from unittest.mock import patch
from firefly_weave.connections.source_bindings import resolve_secret
from firefly_weave.contracts.connectors import ResolvedSecret

called = []
def rejected(index):
    called.append(index)
    return ResolvedSecret(value="rejected")

async def main():
    with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")):
        for index in range(12):
            try:
                await resolve_secret(lambda i=index: rejected(i))
            except RuntimeError:
                pass
            else:
                raise AssertionError("Failed thread start was accepted")
    for _ in range(8):
        assert (await resolve_secret(lambda: ResolvedSecret(value="recovered"))).value == "recovered"
    assert called == [], f"Rejected provider work executed later: {called}"

asyncio.run(main())
""",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_start_failure_after_thread_creation_cannot_run_rejected_work(monkeypatch):
    from firefly_weave.connections.source_bindings import resolve_secret

    original = threading.Thread.start
    threads = []
    called = []

    def start_then_fail(thread):
        threads.append(thread)
        original(thread)
        raise RuntimeError("start failed after creating thread")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", start_then_fail)
        for _ in range(12):
            with pytest.raises(RuntimeError, match="start failed"):
                await resolve_secret(lambda: called.append(True))
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert called == []
    assert (await resolve_secret(lambda: ResolvedSecret(value="recovered"))).value == "recovered"


async def test_cancellation_before_provider_entry_releases_reservation(monkeypatch):
    from firefly_weave.connections.source_bindings import resolve_secret

    original = threading.Thread
    barrier = threading.Event()
    threads = []
    called = []

    def delayed_thread(*, target, **kwargs):
        def delayed():
            barrier.wait(5)
            target()

        thread = original(target=delayed, **kwargs)
        threads.append(thread)
        return thread

    tasks = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading, "Thread", delayed_thread)
            tasks = [asyncio.create_task(resolve_secret(lambda: called.append(True))) for _ in range(4)]
            await asyncio.sleep(0)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(SecretUnavailable):
            await resolve_secret(lambda: ResolvedSecret(value="must-not-start"))
    finally:
        barrier.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert called == []
    assert (await resolve_secret(lambda: ResolvedSecret(value="recovered"))).value == "recovered"


async def test_provider_exception_releases_reservation():
    from firefly_weave.connections.source_bindings import resolve_secret

    def failed():
        raise ValueError("provider failed")

    for _ in range(8):
        with pytest.raises(ValueError, match="provider failed"):
            await resolve_secret(failed)
    assert (await resolve_secret(lambda: ResolvedSecret(value="recovered"))).value == "recovered"


async def test_worker_and_source_share_actual_unfinished_provider_capacity(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.source_bindings import resolve_secret
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.workers import CredentialRequest, LeaseProof
    from firefly_weave.workers.leases import _TaskOperation

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []

    class Provider:
        def resolve(self, handle):
            with lock:
                calls.append(handle)
                if len(calls) == 4:
                    entered.set()
            assert release.wait(10), "Provider barrier was not released"
            return ResolvedSecret(value="private-canary", provider_version="version-1")

    secrets = ScopedSecrets({"test": Provider()}, (SecretGrant(scope, "handle", "test", "locator"),))
    expiry = datetime.now(UTC) + timedelta(seconds=30)
    checked = []

    async def authority(self, request, *, resolved=False, provider_version=None):
        checked.append((resolved, provider_version))
        return "handle", expiry

    monkeypatch.setattr(_TaskOperation, "credential_authority", authority)
    request = CredentialRequest(
        lease=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        connection_revision_id=uuid4(),
        slot="token",
    )
    operations = [_TaskOperation(None, scope, None, None, None, None, secrets) for _ in range(4)]
    tasks = [asyncio.create_task(operation.credentials(request)) for operation in operations]
    try:
        assert await asyncio.to_thread(entered.wait, 3), "Four worker provider calls did not enter"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(SecretUnavailable):
            await resolve_secret(lambda: ResolvedSecret(value="must-not-enter"))
        assert len(calls) == 4
        assert not any(resolved for resolved, _ in checked)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    async with asyncio.timeout(3):
        while True:
            try:
                assert (await resolve_secret(lambda: ResolvedSecret(value="recovered"))).value == "recovered"
                break
            except SecretUnavailable:
                await asyncio.sleep(0.01)
