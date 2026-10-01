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

"""Client identity protocol, durable refresh fences and private credential storage."""

import asyncio
import importlib
import os

import httpx
import pytest


def modules():
    return importlib.import_module("firefly_weave.sdk.auth"), importlib.import_module("firefly_weave.sdk.credentials")


def config(auth):
    return auth.LoginConfig(
        provider_id="test",
        issuer="https://issuer.example",
        client_id="weave-cli",
        target="https://api.example",
        account="local",
        scopes=("openid",),
    )


def discovery(request):
    return httpx.Response(
        200,
        json={
            "issuer": "https://issuer.example",
            "authorization_endpoint": "https://issuer.example/authorize",
            "token_endpoint": "https://issuer.example/token",
            "device_authorization_endpoint": "https://issuer.example/device",
            "revocation_endpoint": "https://issuer.example/revoke",
        },
    )


def test_pkce_callback_state_issuer_path_and_replay():
    auth, _ = modules()
    pending = auth.PKCETransaction("https://issuer.example", "http://127.0.0.1:12345/callback")
    assert "code_challenge=" in pending.authorization_url("https://issuer.example/authorize", "weave-cli", ("openid",))
    assert pending.verifier not in pending.authorization_url("https://issuer.example/authorize", "weave-cli", ())
    for path, query in [
        ("/wrong", {"state": [pending.state], "code": ["code"]}),
        ("/callback", {"state": ["wrong"], "code": ["code"]}),
        ("/callback", {"state": [pending.state, pending.state], "code": ["code"]}),
        ("/callback", {"state": [pending.state], "code": ["code"], "iss": ["https://evil.example"]}),
    ]:
        with pytest.raises(auth.AuthError):
            pending.accept(path, query)
    assert pending.accept("/callback", {"state": [pending.state], "code": ["code"]}) == "code"
    with pytest.raises(auth.AuthError):
        pending.accept("/callback", {"state": [pending.state], "code": ["code"]})


async def test_file_store_atomic_private_and_symlink_rejection(tmp_path):
    _, credentials = modules()
    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    store = credentials.FileCredentialStore(path)
    record = credentials.CredentialRecord(
        binding="binding",
        state="active",
        access_token="access-sentinel",
        refresh_token="refresh-sentinel",
        expires_at=1,
        scopes=("openid",),
    )
    async with store.lock("binding"):
        store.save("binding", record)
        assert store.load("binding").refresh_token.get_secret_value() == "refresh-sentinel"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "sentinel" not in repr(record)
    assert "sentinel" not in str(record)
    with pytest.raises(credentials.CredentialError):
        store.load("other")
    path.chmod(0o644)
    with pytest.raises(credentials.CredentialError):
        store.load("binding")
    path.chmod(0o600)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(credentials.CredentialError):
        credentials.FileCredentialStore(alias).load("binding")
    hard = tmp_path / "hard.json"
    os.link(path, hard)
    with pytest.raises(credentials.CredentialError):
        store.load("binding")


async def test_refresh_rotation_concurrency_and_logout_fence(tmp_path):
    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "tokens.json")
    calls = []

    def receive(request):
        if request.method == "GET":
            return discovery(request)
        calls.append(request.content)
        persisted = store.load(settings.binding)
        assert persisted.state == "refreshing" and persisted.refresh_token is None
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "openid",
            },
        )

    store.save(
        settings.binding,
        credentials.CredentialRecord(
            binding=settings.binding,
            state="active",
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=0,
            scopes=("openid",),
        ),
    )
    session = auth.OAuthSession(settings, store, transport_factory=lambda: httpx.MockTransport(receive))
    assert await asyncio.gather(*(session.get_access_token(settings.target) for _ in range(5))) == ["new-access"] * 5
    assert len(calls) == 1 and b"old-refresh" in calls[0]
    assert store.load(settings.binding).refresh_token.get_secret_value() == "new-refresh"
    await session.logout()
    assert store.load(settings.binding) is None
    await session.logout()
    with pytest.raises(auth.AuthError):
        await session.get_access_token(settings.target)


async def test_refresh_ambiguous_timeout_leaves_durable_token_free_fence(tmp_path):
    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "tokens.json")
    store.save(
        settings.binding,
        credentials.CredentialRecord(
            binding=settings.binding,
            state="active",
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=0,
            scopes=("openid",),
        ),
    )
    calls = []

    def receive(request):
        if request.method == "GET":
            return discovery(request)
        calls.append(request)
        raise httpx.ReadTimeout("secret-sentinel")

    for _ in range(2):
        session = auth.OAuthSession(settings, store, transport_factory=lambda: httpx.MockTransport(receive))
        with pytest.raises(auth.AuthError) as failure:
            await session.get_access_token(settings.target)
        assert "secret-sentinel" not in str(failure.value)
    assert len(calls) == 1
    record = store.load(settings.binding)
    assert record.state == "refreshing" and record.access_token is None and record.refresh_token is None


async def test_untrusted_discovery_and_target_do_not_disclose_credentials(tmp_path):
    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "issuer": "https://issuer.example",
                "token_endpoint": "https://evil.example/token",
                "authorization_endpoint": "https://issuer.example/authorize",
            },
        )

    session = auth.OAuthSession(
        settings,
        credentials.FileCredentialStore(tmp_path / "tokens.json"),
        transport_factory=lambda: httpx.MockTransport(receive),
    )
    with pytest.raises(auth.AuthError):
        await session.discover()
    with pytest.raises(auth.AuthError):
        await session.get_access_token("https://evil.example")
    assert len(calls) == 1 and "authorization" not in calls[0].headers


def test_native_store_never_silently_selects_insecure_backend(tmp_path):
    _, credentials = modules()

    class Insecure:
        priority = 99

    with pytest.raises(credentials.CredentialError):
        credentials.NativeCredentialStore(backend=Insecure(), lock_directory=tmp_path)


async def test_pending_device_login_cannot_recreate_logged_out_credentials(tmp_path):
    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "tokens.json")
    ready = asyncio.Event()

    async def receive(request):
        if request.method == "GET":
            return discovery(request)
        if request.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "user",
                    "verification_uri": "https://issuer.example/verify",
                    "expires_in": 60,
                    "interval": 0.01,
                },
            )
        await ready.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "late-access",
                "refresh_token": "late-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
            },
        )

    session = auth.OAuthSession(settings, store, transport_factory=lambda: httpx.MockTransport(receive))

    async def logout():
        await session.logout()
        ready.set()

    with pytest.raises(auth.AuthError, match="SUPERSEDED"):
        await session.login(flow="device", instructions=lambda *_: asyncio.create_task(logout()))
    assert store.load(settings.binding) is None


@pytest.mark.parametrize("phase, requests", [("refreshing", 0), ("active", 1)])
async def test_refresh_persistence_failure_never_reuses_old_refresh(tmp_path, phase, requests):
    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    base = credentials.FileCredentialStore(tmp_path / "tokens.json")
    base.save(
        settings.binding,
        credentials.CredentialRecord(
            binding=settings.binding,
            state="active",
            access_token="old",
            refresh_token="old-refresh",
            expires_at=0,
            scopes=("openid",),
        ),
    )

    class FailingStore(credentials.FileCredentialStore):
        def save(self, binding, record):
            if record.state == phase:
                raise credentials.CredentialError()
            super().save(binding, record)

    calls = []

    def receive(request):
        if request.method == "GET":
            return discovery(request)
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "openid",
            },
        )

    session = auth.OAuthSession(
        settings, FailingStore(base.path), transport_factory=lambda: httpx.MockTransport(receive)
    )
    with pytest.raises(credentials.CredentialError):
        await session.get_access_token(settings.target)
    assert len(calls) == requests
    if phase == "active":
        with pytest.raises(auth.AuthError):
            await auth.OAuthSession(settings, base).get_access_token(settings.target)
        assert base.load(settings.binding).refresh_token is None


async def test_pkce_listener_rejects_wrong_host_and_closes_accepted_connections_on_cancel(tmp_path):
    from urllib.parse import parse_qs, urlencode, urlsplit

    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth).model_copy(update={"login_timeout": 0.15})
    readers = []
    writers = []
    response_statuses = []

    async def browser(url):
        query = parse_qs(urlsplit(url).query)
        redirect = urlsplit(query["redirect_uri"][0])
        reader, writer = await asyncio.open_connection(redirect.hostname, redirect.port)
        target = redirect.path + "?" + urlencode({"state": query["state"][0], "code": "code"})
        writer.write(f"GET {target} HTTP/1.1\r\nHost: evil.example\r\n\r\n".encode())
        await writer.drain()
        response_statuses.append((await reader.read()).split(b"\r\n")[0])
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection(redirect.hostname, redirect.port)
        writer.write(b"GET /callback HTTP/1.1\r\n")
        await writer.drain()
        readers.append(reader)
        writers.append(writer)

    session = auth.OAuthSession(
        settings,
        credentials.FileCredentialStore(tmp_path / "tokens.json"),
        transport_factory=lambda: httpx.MockTransport(discovery),
    )
    try:
        with pytest.raises(auth.AuthError, match="EXPIRED"):
            await session.login(flow="pkce", browser=browser)
        assert response_statuses == [b"HTTP/1.1 400 Bad Request"]
        assert await asyncio.wait_for(readers[0].read(), 0.5) == b""
    finally:
        for writer in writers:
            writer.close()
            await writer.wait_closed()


async def test_cooperating_processes_rotate_once_and_interrupted_fence_is_durable(tmp_path):
    import sys

    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    path = tmp_path / "process-tokens.json"
    store = credentials.FileCredentialStore(path)
    store.save(
        settings.binding,
        credentials.CredentialRecord(
            binding=settings.binding,
            state="active",
            access_token="old",
            refresh_token="old-refresh",
            expires_at=0,
            scopes=("openid",),
        ),
    )
    script = """
import asyncio, sys
from pathlib import Path
import httpx
from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.credentials import FileCredentialStore
config = LoginConfig(provider_id="test", issuer="https://issuer.example", client_id="weave-cli",
                     target="https://api.example", account="local", scopes=("openid",))
path = Path(sys.argv[1])
async def receive(request):
    if request.method == "GET":
        return httpx.Response(200, json={"issuer":config.issuer,
            "authorization_endpoint":config.issuer+"/authorize", "token_endpoint":config.issuer+"/token"})
    with (path.parent / "exchanges").open("a") as out:
        out.write("exchange\\n")
    await asyncio.sleep(.05)
    return httpx.Response(200,json={"access_token":"new", "refresh_token":"new-refresh",
        "token_type":"Bearer", "expires_in":300, "scope":"openid"})
async def main():
    session=OAuthSession(config,FileCredentialStore(path),transport_factory=lambda:httpx.MockTransport(receive))
    assert await session.get_access_token(config.target)=="new"
asyncio.run(main())
"""
    children = [
        await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        for _ in range(4)
    ]
    completed = await asyncio.gather(*(asyncio.wait_for(child.communicate(), 15) for child in children))
    for child, (output, error) in zip(children, completed, strict=True):
        assert child.returncode == 0 and not output and not error, error.decode()
    assert (tmp_path / "exchanges").read_text() == "exchange\n"
    interrupted = """
import asyncio, os, sys
from pathlib import Path
from firefly_weave.sdk.credentials import FileCredentialStore, CredentialRecord
async def main():
    store=FileCredentialStore(Path(sys.argv[1]))
    async with store.lock(sys.argv[2]):
        store.save(sys.argv[2], CredentialRecord(binding=sys.argv[2], state="refreshing"))
        os._exit(0)
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", interrupted, str(path), settings.binding)
    assert await child.wait() == 0
    with pytest.raises(auth.AuthError):
        await auth.OAuthSession(settings, store).get_access_token(settings.target)
    assert store.load(settings.binding).refresh_token is None


async def test_new_login_generation_defeats_previous_completion(tmp_path):
    from pyfly.oauth2 import OAuth2Tokens

    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "tokens.json")
    first = credentials.CredentialRecord(binding=settings.binding, state="authenticating")
    second = credentials.CredentialRecord(binding=settings.binding, state="authenticating")
    store.save(settings.binding, second)
    tokens = OAuth2Tokens(access_token="access", token_type="Bearer", expires_in=300)
    session = auth.OAuthSession(settings, store)
    with pytest.raises(auth.AuthError, match="SUPERSEDED"):
        await session._save_login(tokens, first.generation)
    assert store.load(settings.binding).generation == second.generation
    assert (await session._save_login(tokens, second.generation))["authenticated"]


@pytest.mark.parametrize("denied", [False, True])
async def test_device_pkce_public_polling_slow_down_or_denial(monkeypatch, tmp_path, denied):
    from urllib.parse import parse_qs

    import pyfly.oauth2

    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "device.json")
    original = pyfly.oauth2.OAuth2Client
    elapsed, sleeps, calls = [0.0], [], []
    challenges = []

    async def sleep(delay):
        sleeps.append(delay)
        elapsed[0] += delay

    class TimedClient(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, clock=lambda: elapsed[0], sleep=sleep)

    monkeypatch.setattr(pyfly.oauth2, "OAuth2Client", TimedClient)

    def receive(request):
        if request.method == "GET":
            return discovery(request)
        form = parse_qs(request.content.decode())
        if request.url.path == "/device":
            assert form["code_challenge_method"] == ["S256"] and "code_verifier" not in form
            challenges.append(form["code_challenge"][0])
            return httpx.Response(
                200,
                json={
                    "device_code": "opaque",
                    "user_code": "user",
                    "verification_uri": "https://issuer.example/verify",
                    "expires_in": 120,
                    "interval": 1,
                },
            )
        assert pyfly.oauth2.pkce_challenge(form["code_verifier"][0]) == challenges[0]
        calls.append(form)
        if denied:
            return httpx.Response(400, json={"error": "access_denied", "error_description": "secret-sentinel"})
        if len(calls) <= 2:
            return httpx.Response(400, json={"error": "authorization_pending" if len(calls) == 1 else "slow_down"})
        return httpx.Response(
            200, json={"access_token": "access", "refresh_token": "refresh", "token_type": "Bearer", "expires_in": 300}
        )

    session = auth.OAuthSession(settings, store, transport_factory=lambda: httpx.MockTransport(receive))
    if denied:
        with pytest.raises(auth.AuthError) as failure:
            await session.login(flow="device")
        assert failure.value.code == "WV-AUTH-DENIED" and "sentinel" not in str(failure.value)
        assert store.load(settings.binding).access_token is None and len(calls) == 1
    else:
        assert (await session.login(flow="device"))["authenticated"]
        assert sleeps == [1, 1, 6]
        assert len({form["code_verifier"][0] for form in calls}) == 1


async def test_logout_during_discovery_prevents_late_login_completion(monkeypatch, tmp_path):
    from pyfly.oauth2 import OAuth2Tokens

    auth, credentials = modules()
    tmp_path.chmod(0o700)
    settings = config(auth)
    store = credentials.FileCredentialStore(tmp_path / "discover-login.json")
    session = auth.OAuthSession(settings, store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def discover():
        entered.set()
        await release.wait()
        return {
            "issuer": settings.issuer,
            "token_endpoint": settings.issuer + "/token",
            "authorization_endpoint": settings.issuer + "/authorize",
        }

    async def complete(*args):
        return OAuth2Tokens(access_token="new", token_type="Bearer", expires_in=300)

    monkeypatch.setattr(session, "discover", discover)
    monkeypatch.setattr(session, "_pkce", complete)
    pending = asyncio.create_task(session.login(flow="pkce"))
    await asyncio.wait_for(entered.wait(), 1)
    await session.logout()
    release.set()
    with pytest.raises(auth.AuthError, match="SUPERSEDED"):
        await asyncio.wait_for(pending, 1)
    assert store.load(settings.binding) is None


@pytest.fixture
def win32_native_stores(monkeypatch, tmp_path):
    """Win32 mutex semantics shim; no native Windows/vault execution is claimed."""
    import ctypes
    import threading
    from types import SimpleNamespace

    _, credentials = modules()
    values, handles, mutexes = {}, {}, {}
    operations = []

    class WinVaultKeyring:
        __module__ = "keyring.backends.Windows"

        def get_password(self, service, binding):
            return values.get((service, binding))

        def set_password(self, service, binding, value):
            values[service, binding] = value

        def delete_password(self, service, binding):
            del values[service, binding]

    stores = [credentials.NativeCredentialStore(backend=WinVaultKeyring(), lock_directory=tmp_path) for _ in range(2)]

    def create(_, initially_owned, name):
        assert not initially_owned
        handle = len(operations) + 1
        operations.append(("create", threading.get_ident()))
        handles[handle] = name
        return handle

    def wait(handle, timeout):
        assert timeout == 0
        name, thread = handles[handle], threading.get_ident()
        owner, depth = mutexes.get(name, (None, 0))
        if owner not in (None, thread):
            return 0x102
        mutexes[name] = (thread, depth + 1)
        operations.append(("acquire", thread))
        return 0

    def release(handle):
        name, thread = handles[handle], threading.get_ident()
        owner, depth = mutexes[name]
        assert owner == thread and depth > 0
        mutexes[name] = (thread if depth > 1 else None, depth - 1)
        operations.append(("release", thread))
        return 1

    def close(handle):
        del handles[handle]
        return 1

    kernel = SimpleNamespace(CreateMutexW=create, WaitForSingleObject=wait, ReleaseMutex=release, CloseHandle=close)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(credentials, "os", SimpleNamespace(name="nt"))
    yield stores, operations
    assert not handles and all(depth == 0 for _, depth in mutexes.values())


@pytest.mark.parametrize("phase", ["discovery", "exchange"])
@pytest.mark.parametrize("contender", ["refresh", "logout"])
async def test_windows_thread_reentry_cannot_bypass_task_exclusion(win32_native_stores, contender, phase):
    auth, credentials = modules()
    stores, operations = win32_native_stores
    settings = config(auth)
    stores[0].save(
        settings.binding,
        credentials.CredentialRecord(
            binding=settings.binding,
            state="active",
            access_token="old",
            refresh_token="old-refresh",
            expires_at=0,
            scopes=("openid",),
        ),
    )
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    discoveries, exchanges = [], []

    async def receive(request):
        if request.method == "GET":
            discoveries.append(1)
            if phase == "discovery":
                entered.set()
                await release.wait()
            return discovery(request)
        exchanges.append(1)
        if phase == "exchange":
            entered.set()
            await release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "rotated",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "openid",
            },
        )

    sessions = [
        auth.OAuthSession(settings, store, transport_factory=lambda: httpx.MockTransport(receive)) for store in stores
    ]
    first = asyncio.create_task(sessions[0].get_access_token(settings.target))
    await asyncio.wait_for(entered.wait(), 1)

    async def contend():
        attempted.set()
        return await (sessions[1].logout() if contender == "logout" else sessions[1].get_access_token(settings.target))

    second = asyncio.create_task(contend())
    await asyncio.wait_for(attempted.wait(), 1)
    try:
        assert not second.done(), "Contender bypassed suspended refresh"
        assert len(discoveries) == 1, "Second refresh reused the old record during discovery"
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert results[0] == "new" and len(exchanges) == 1
    assert len({thread for operation, thread in operations if operation in {"acquire", "release"}}) == 1
    if contender == "logout":
        assert stores[0].load(settings.binding) is None
    else:
        assert results[1] == "new" and stores[0].load(settings.binding).refresh_token.get_secret_value() == "rotated"


async def test_windows_waiter_cancellation_keeps_shared_lock_live(win32_native_stores):
    stores, operations = win32_native_stores
    started = asyncio.Event()

    async def wait():
        started.set()
        async with stores[1].lock("cancelled-record"):
            raise AssertionError("Contender entered the held lock")

    async with stores[0].lock("cancelled-record"):
        pending = asyncio.create_task(wait())
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert sum(operation == "acquire" for operation, _ in operations) == 1
    async with stores[1].lock("cancelled-record"):
        assert sum(operation == "acquire" for operation, _ in operations) == 2


async def test_windows_native_lock_shared_across_event_loop_threads(win32_native_stores, monkeypatch):
    import threading

    _, credentials = modules()
    stores, operations = win32_native_stores
    waiting, entered = threading.Event(), threading.Event()
    failures = []
    original_sleep = asyncio.sleep

    async def observed_sleep(delay):
        waiting.set()
        await original_sleep(delay)

    monkeypatch.setattr(credentials.asyncio, "sleep", observed_sleep)

    def worker():
        async def acquire():
            async with stores[1].lock("thread-record"):
                entered.set()

        try:
            asyncio.run(acquire())
        except BaseException as error:
            failures.append(type(error).__name__)

    thread = threading.Thread(target=worker)
    async with stores[0].lock("thread-record"):
        thread.start()
        assert await asyncio.to_thread(waiting.wait, 1)
        assert not entered.is_set()
        assert sum(operation == "create" for operation, _ in operations) == 1
    await asyncio.to_thread(thread.join, 2)
    assert not thread.is_alive() and not failures and entered.is_set()
    acquiring = [thread for operation, thread in operations if operation == "acquire"]
    releasing = [thread for operation, thread in operations if operation == "release"]
    assert len(set(acquiring)) == 2 and acquiring == releasing


async def test_windows_task_exclusion_deadline_does_not_acquire_mutex(win32_native_stores, monkeypatch):
    stores, operations = win32_native_stores
    _, credentials = modules()
    original_timeout = asyncio.timeout
    async with stores[0].lock("deadline-record"):
        with monkeypatch.context() as patch:
            patch.setattr(credentials.asyncio, "timeout_at", lambda _: original_timeout(0.01))
            with pytest.raises(credentials.CredentialError):
                async with stores[1].lock("deadline-record"):
                    raise AssertionError("Deadline waiter entered lock")
        assert sum(operation == "create" for operation, _ in operations) == 1
    async with stores[1].lock("deadline-record"):
        assert sum(operation == "create" for operation, _ in operations) == 2


async def test_windows_both_acquisition_stages_share_one_deadline(win32_native_stores, monkeypatch):
    _, credentials = modules()
    stores, _ = win32_native_stores
    deadlines = []
    original = asyncio.timeout_at

    def observed(deadline):
        deadlines.append(deadline)
        return original(deadline)

    monkeypatch.setattr(credentials.asyncio, "timeout_at", observed)
    async with stores[0].lock("one-deadline"):
        assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
