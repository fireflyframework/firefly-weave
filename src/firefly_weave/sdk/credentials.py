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

"""Explicit native or private POSIX storage with cooperating-process locks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from _thread import LockType
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

MAX_RECORD_BYTES = 65536
_NATIVE_TASK_LOCKS: WeakValueDictionary[str, LockType] = WeakValueDictionary()
_NATIVE_TASK_LOCKS_GUARD = Lock()


@asynccontextmanager
async def _task_exclusion(name: str, deadline: float) -> AsyncIterator[None]:
    # Keep a strong reference while holding or waiting; registry eviction cannot
    # split one record's exclusion across instances, event loops or threads.
    with _NATIVE_TASK_LOCKS_GUARD:
        lock = _NATIVE_TASK_LOCKS.get(name)
        if lock is None:
            lock = Lock()
            _NATIVE_TASK_LOCKS[name] = lock
    acquired = False
    try:
        try:
            async with asyncio.timeout_at(deadline):
                while not lock.acquire(blocking=False):
                    await asyncio.sleep(0.025)
                acquired = True
        except TimeoutError:
            raise CredentialError() from None
        yield
    finally:
        if acquired:
            lock.release()


class CredentialError(Exception):
    def __init__(self) -> None:
        super().__init__("Secure credential storage is unavailable; select and verify an explicit store")


class CredentialRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    binding: str
    generation: UUID = Field(default_factory=uuid4)
    state: Literal["active", "authenticating", "refreshing", "logged_out"]
    access_token: SecretStr | None = Field(default=None, repr=False)
    refresh_token: SecretStr | None = Field(default=None, repr=False)
    expires_at: float = 0
    scopes: tuple[str, ...] = ()


def encode(record: CredentialRecord) -> str:
    value = record.model_dump(mode="json")
    for key in ("access_token", "refresh_token"):
        token = getattr(record, key)
        value[key] = token.get_secret_value() if token else None
    raw = json.dumps(value, separators=(",", ":"))
    if len(raw.encode()) > MAX_RECORD_BYTES:
        raise CredentialError()
    return raw


def decode(raw: str, binding: str) -> CredentialRecord:
    try:
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise ValueError()
        record = CredentialRecord.model_validate_json(raw)
        if record.binding != binding:
            raise ValueError()
        return record
    except (ValueError, ValidationError):
        raise CredentialError() from None


class CredentialStore(Protocol):
    def load(self, binding: str) -> CredentialRecord | None: ...
    def save(self, binding: str, record: CredentialRecord) -> None: ...
    def delete(self, binding: str) -> None: ...
    def lock(self, binding: str) -> Any: ...


def _directory(path: Path) -> int:
    """Walk from root with descriptor-relative no-follow opens; anchor final parent."""
    if os.name != "posix" or not path.is_absolute() or ".." in path.parts:
        raise CredentialError()
    descriptor = -1
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CredentialError()
        return descriptor
    except (OSError, CredentialError):
        if descriptor >= 0:
            os.close(descriptor)
        raise CredentialError() from None


def _check_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise CredentialError()


class FileCredentialStore:
    def __init__(self, path: Path) -> None:
        if os.name != "posix" or not path.is_absolute() or path.name in {"", ".", ".."}:
            raise CredentialError()
        self.path = path

    def load(self, binding: str) -> CredentialRecord | None:
        directory = _directory(self.path.parent)
        try:
            return self._read(directory, binding)
        finally:
            os.close(directory)

    def _read(self, directory: int, binding: str) -> CredentialRecord | None:
        descriptor = -1
        try:
            try:
                descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            _check_file(descriptor)
            raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
            return decode(raw.decode("utf-8"), binding)
        except (OSError, UnicodeError):
            raise CredentialError() from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def save(self, binding: str, record: CredentialRecord) -> None:
        if record.binding != binding:
            raise CredentialError()
        raw = encode(record).encode()
        directory = _directory(self.path.parent)
        temporary = ".weave-" + secrets.token_hex(16)
        descriptor = -1
        created = False
        try:
            self._read(directory, binding)
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            created = True
            _check_file(descriptor)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(descriptor)
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            created = False
            os.fsync(directory)
        except OSError:
            raise CredentialError() from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if created:
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)

    def delete(self, binding: str) -> None:
        directory = _directory(self.path.parent)
        try:
            if self._read(directory, binding) is not None:
                os.unlink(self.path.name, dir_fd=directory)
                os.fsync(directory)
        except OSError:
            raise CredentialError() from None
        finally:
            os.close(directory)

    @asynccontextmanager
    async def lock(self, binding: str) -> AsyncIterator[None]:
        if os.name != "posix":
            raise CredentialError()
        import fcntl

        directory = _directory(self.path.parent)
        descriptor = -1
        try:
            # One lock for the physical record, including attempts with different bindings.
            try:
                descriptor = os.open(
                    self.path.name + ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=directory,
                )
            except FileExistsError:
                descriptor = os.open(
                    self.path.name + ".lock",
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            _check_file(descriptor)
            async with asyncio.timeout(60):
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.025)
            yield
        except (OSError, TimeoutError):
            raise CredentialError() from None
        finally:
            if descriptor >= 0:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            os.close(directory)


def _approved(backend: Any) -> bool:
    identity = (type(backend).__module__, type(backend).__name__)
    if identity == ("keyring.backends.chainer", "ChainerBackend"):
        return bool(backend.backends) and all(_approved(item) for item in backend.backends)
    return identity in {
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    }


class NativeCredentialStore:
    def __init__(self, *, backend: Any = None, lock_directory: Path | None = None) -> None:
        try:
            if backend is None:
                import keyring

                backend = keyring.get_keyring()
            if not _approved(backend):
                raise CredentialError()
            self.backend = backend
            self.directory = lock_directory or Path.home() / ".weave-credential-locks"
            if os.name == "posix":
                self.directory.mkdir(mode=0o700, exist_ok=True)
                descriptor = _directory(self.directory)
                os.close(descriptor)
        except Exception:
            raise CredentialError() from None

    def load(self, binding: str) -> CredentialRecord | None:
        try:
            value = self.backend.get_password("firefly-weave", binding)
            return decode(value, binding) if value is not None else None
        except Exception:
            raise CredentialError() from None

    def save(self, binding: str, record: CredentialRecord) -> None:
        if record.binding != binding:
            raise CredentialError()
        try:
            self.backend.set_password("firefly-weave", binding, encode(record))
        except Exception:
            raise CredentialError() from None

    def delete(self, binding: str) -> None:
        try:
            if self.load(binding) is not None:
                self.backend.delete_password("firefly-weave", binding)
        except Exception:
            raise CredentialError() from None

    @asynccontextmanager
    async def lock(self, binding: str) -> AsyncIterator[None]:
        if os.name == "posix":
            path = self.directory / hashlib.sha256(binding.encode()).hexdigest()
            async with FileCredentialStore(path).lock(binding):
                yield
            return
        if os.name != "nt":
            raise CredentialError()
        # Windows mutexes reenter on the same thread; task exclusion must come first.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\FireflyWeave-" + hashlib.sha256((str(Path.home()) + binding).encode()).hexdigest()
        deadline = asyncio.get_running_loop().time() + 60
        async with _task_exclusion(name, deadline):
            handle = kernel.CreateMutexW(None, False, name)
            if not handle:
                raise CredentialError()
            acquired = False
            try:
                try:
                    async with asyncio.timeout_at(deadline):
                        while True:
                            status = kernel.WaitForSingleObject(handle, 0)
                            if status in (0, 0x80):
                                acquired = True
                                break
                            if status != 0x102:
                                raise CredentialError()
                            await asyncio.sleep(0.025)
                except TimeoutError:
                    raise CredentialError() from None
                yield
            finally:
                if acquired:
                    kernel.ReleaseMutex(handle)
                kernel.CloseHandle(handle)
