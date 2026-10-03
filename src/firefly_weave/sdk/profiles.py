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

"""Named, nonsecret platform profiles shared by the CLI, the Studio host and the desktop shell.

A profile pins one server origin, the reviewed sign-in settings for it, the
sign-in flows the administrator allows, the chosen workspace and the last
signed-in account hint. Tokens never enter this file: credentials stay in the
native credential store (or an explicit private file store) under the
profile's login binding. Every read-modify-write runs under a cross-process
lock and replaces the file atomically; a file that does not parse is reported,
never silently rewritten. Studio's own small preferences file (`studio.json`)
lives next to `profiles.json` under the same rules.
"""

from __future__ import annotations

import importlib
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from firefly_weave.sdk.auth import AuthError, LoginConfig

if TYPE_CHECKING:
    from firefly_weave.studio.service import StudioProfile

STORE_FILE = "profiles.json"
PREFERENCES_FILE = "studio.json"
MAX_STORE_BYTES = 1024 * 1024
MAX_PREFERENCES_BYTES = 16 * 1024
MAX_PROFILES = 64
LOCK_TIMEOUT = 10.0
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
MESSAGES = {
    "WV-PROFILE-NOT-FOUND": "No saved platform has that name.",
    "WV-PROFILE-EXISTS": "A saved platform already uses that name.",
    "WV-PROFILE-NAME": (
        "Use 1 to 64 letters, digits, spaces, dots, hyphens or underscores, starting with a letter or digit."
    ),
    "WV-PROFILE-STORE": "The saved platforms file cannot be read or written safely.",
    "WV-PROFILE-NONE": "No platform is selected.",
    "WV-PROFILE-CHANGED": "The server now announces different sign-in settings; review them before continuing.",
    "WV-PROFILE-ACCOUNT": "That account belongs to a different identity provider than this platform uses.",
}
_Result = TypeVar("_Result")
SignInFlow = Literal["browser", "device"]


class ProfileError(Exception):
    """Stable profile failure; the message is plain language and never includes file content."""

    exit_code = 2

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or MESSAGES.get(code, "The platform profile request failed.")
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(UTC)


def profile_name(value: Any) -> str:
    """Return a valid profile name or raise `WV-PROFILE-NAME`; names double as the login account."""
    if not isinstance(value, str) or value.endswith(" ") or not PROFILE_NAME.fullmatch(value):
        raise ProfileError("WV-PROFILE-NAME")
    return value


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is not None and (
        not value or len(value) > limit or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)
    ):
        raise ValueError("A bounded printable value is required")
    return value


def default_config_dir() -> Path:
    """`$WEAVE_CONFIG_HOME` (absolute) or the platform's per-user configuration directory."""
    configured = os.environ.get("WEAVE_CONFIG_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ProfileError("WV-PROFILE-STORE", "WEAVE_CONFIG_HOME must be an absolute path.")
        return path
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata and Path(appdata).is_absolute() else Path.home() / "AppData" / "Roaming"
        return base / "Firefly Weave"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefly Weave"
    # XDG: a relative value is invalid and must be ignored.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "firefly-weave"


class WorkspaceSelection(BaseModel):
    """The tenant, project and environment chosen for a profile, with names for display only."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: UUID
    project_id: UUID
    environment_id: UUID
    tenant_name: str | None = None
    project_name: str | None = None
    environment_name: str | None = None

    @field_validator("tenant_name", "project_name", "environment_name")
    @classmethod
    def label(cls, value: str | None) -> str | None:
        return _bounded_text(value, 200)


class AccountHint(BaseModel):
    """Last signed-in account for display and linking help; never used for authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    subject: str
    display_name: str | None = None
    issuer: str = Field(min_length=1, max_length=2048)
    provider_id: str = Field(min_length=1, max_length=200)

    @field_validator("subject")
    @classmethod
    def bounded_subject(cls, value: str) -> str:
        return _bounded_text(value, 255) or ""

    @field_validator("display_name")
    @classmethod
    def bounded_name(cls, value: str | None) -> str | None:
        return _bounded_text(value, 200)


def _all_flows() -> list[SignInFlow]:
    return ["browser", "device"]


class PlatformProfile(BaseModel):
    """One saved platform: server origin, pinned login settings, allowed flows, workspace and account hint."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    login: LoginConfig
    source: Literal["server", "file", "manual"]
    display_name: str | None = None
    provider_name: str | None = None
    # The administrator's allowed sign-in flows (server profiles); connection files allow both.
    flows: list[SignInFlow] = Field(default_factory=_all_flows, min_length=1, max_length=2)
    workspace: WorkspaceSelection | None = None
    account: AccountHint | None = None
    credential_store: Literal["native", "file"] = "native"
    credential_file: Path | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        try:
            return profile_name(value)
        except ProfileError:
            raise ValueError(MESSAGES["WV-PROFILE-NAME"]) from None

    @field_validator("display_name", "provider_name")
    @classmethod
    def label(cls, value: str | None) -> str | None:
        return _bounded_text(value, 100)

    @field_validator("flows")
    @classmethod
    def unique_flows(cls, value: list[SignInFlow]) -> list[SignInFlow]:
        if len(set(value)) != len(value):
            raise ValueError("Each sign-in flow may appear once")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamps must include a time zone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent(self) -> PlatformProfile:
        if self.login.account != self.name:
            # The name is part of the credential binding, so one profile owns one credential record.
            raise ValueError("The login account must equal the profile name")
        if (self.credential_store == "file") != (self.credential_file is not None):
            raise ValueError("A file credential store requires an explicit credential file, and only then")
        if self.credential_file is not None and not self.credential_file.is_absolute():
            raise ValueError("The credential file must be an absolute path")
        if self.account is not None and (
            self.account.issuer != self.login.issuer or self.account.provider_id != self.login.provider_id
        ):
            raise ValueError("The account hint must come from this profile's identity provider")
        return self

    @property
    def server(self) -> str:
        """The exact API origin; always equal to `login.target`."""
        return self.login.target

    @classmethod
    def from_studio_profile(
        cls,
        studio: StudioProfile,
        login: LoginConfig,
        *,
        source: Literal["server", "file", "manual"] = "file",
    ) -> PlatformProfile:
        """Adopt a legacy Studio profile; its name becomes the login account (a new credential binding).

        A partial legacy scope is dropped because a workspace selection names
        all three levels. Only attributes are read, so Studio is not imported.
        """
        name = profile_name(studio.name)
        if login.target != studio.base_url.rstrip("/"):
            raise ValueError("The login target must equal the legacy profile origin")
        if login.account != name:
            login = LoginConfig.model_validate({**login.model_dump(), "account": name})
        workspace = None
        if studio.tenant_id and studio.project_id and studio.environment_id:
            workspace = WorkspaceSelection(
                tenant_id=studio.tenant_id, project_id=studio.project_id, environment_id=studio.environment_id
            )
        credential_store: Literal["native", "file"] = "file" if studio.credential_store == "file" else "native"
        return cls(
            name=name,
            login=login,
            source=source,
            workspace=workspace,
            credential_store=credential_store,
            credential_file=studio.credential_file if credential_store == "file" else None,
        )

    def to_studio_profile(self) -> StudioProfile:
        """Legacy Studio view of this profile; imported lazily so CLI paths never load Studio."""
        from firefly_weave.studio.service import StudioProfile

        return StudioProfile(
            name=self.name,
            base_url=self.server,
            tenant_id=self.workspace.tenant_id if self.workspace else None,
            project_id=self.workspace.project_id if self.workspace else None,
            environment_id=self.workspace.environment_id if self.workspace else None,
            credential_store=self.credential_store,
            credential_file=self.credential_file,
        )


class ProfileDocument(BaseModel):
    """The whole `profiles.json` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    active: str | None = None
    profiles: dict[str, PlatformProfile] = Field(default_factory=dict, max_length=MAX_PROFILES)

    @model_validator(mode="after")
    def consistent(self) -> ProfileDocument:
        if any(key != value.name for key, value in self.profiles.items()):
            raise ValueError("Each profile must be stored under its own name")
        if len({key.casefold() for key in self.profiles}) != len(self.profiles):
            raise ValueError("Profile names must differ by more than letter case")
        if self.active is not None and self.active not in self.profiles:
            raise ValueError("The active profile must exist")
        return self


class StudioPreferences(BaseModel):
    """Studio's start preference: ask on first run, or start working locally without the choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    start: Literal["ask", "local"] = "ask"


def _store_error(message: str | None = None) -> ProfileError:
    return ProfileError("WV-PROFILE-STORE", message)


def _windows() -> bool:
    return os.name == "nt"


def _read_bounded(descriptor: int, limit: int, oversized: str) -> bytes:
    raw = bytearray()
    while len(raw) <= limit:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return bytes(raw)
        raw.extend(chunk)
    raise _store_error(oversized)


def _try_lock(descriptor: int) -> bool:
    if _windows():
        msvcrt: Any = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(descriptor: int) -> None:
    if _windows():
        msvcrt: Any = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


class ProfileStore:
    """Private `profiles.json` with atomic replacement and a cross-process lock around every change.

    POSIX: directory 0700 and files 0600 (created or tightened), owner-only,
    no symlinks or hard links, descriptor-relative operations. Windows relies
    on the per-user profile ACL, refuses symlinks and junctions, and locks with
    msvcrt. Calls block for at most `LOCK_TIMEOUT` seconds while another
    process holds the lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_config_dir() / STORE_FILE
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."}:
            raise _store_error("The saved platforms file needs an absolute path.")

    @property
    def location(self) -> str:
        return str(self.path)

    # --- reads -------------------------------------------------------------------------------------------------

    def load(self) -> ProfileDocument:
        """Read without locking (replacement is atomic); a missing store is empty and is not created."""
        try:
            with self._directory(create=False) as directory:
                return self._read(directory)
        except FileNotFoundError:
            return ProfileDocument()
        except OSError:
            raise _store_error() from None

    def names(self) -> list[str]:
        return sorted(self.load().profiles)

    def get(self, name: str) -> PlatformProfile:
        """The profile with this name, ignoring letter case (names are unique ignoring case)."""
        profile_name(name)
        return _existing(self.load(), name)

    def active(self) -> PlatformProfile | None:
        document = self.load()
        return document.profiles[document.active] if document.active is not None else None

    def resolve(self, name: str | None = None) -> PlatformProfile:
        """The named profile, else the active one; `WV-PROFILE-NONE` when nothing is selected."""
        if name is not None:
            return self.get(name)
        selected = self.active()
        if selected is None:
            raise ProfileError("WV-PROFILE-NONE")
        return selected

    # --- changes -----------------------------------------------------------------------------------------------

    def save(self, profile: PlatformProfile, *, activate: bool = False, replace: bool = False) -> PlatformProfile:
        def change(document: ProfileDocument) -> tuple[ProfileDocument, PlatformProfile]:
            existing = document.profiles.get(profile.name)
            if any(key.casefold() == profile.name.casefold() and key != profile.name for key in document.profiles):
                raise ProfileError("WV-PROFILE-EXISTS")
            if existing is not None and not replace:
                raise ProfileError("WV-PROFILE-EXISTS")
            if existing is None and len(document.profiles) >= MAX_PROFILES:
                raise _store_error(f"At most {MAX_PROFILES} platforms can be saved.")
            stored = _updated(profile, created_at=existing.created_at if existing else profile.created_at)
            profiles = {**document.profiles, profile.name: stored}
            return _document(profile.name if activate else document.active, profiles), stored

        return self._mutate(change)

    def activate(self, name: str) -> PlatformProfile:
        profile_name(name)

        def change(document: ProfileDocument) -> tuple[ProfileDocument, PlatformProfile]:
            selected = _existing(document, name)
            return _document(selected.name, document.profiles), selected

        return self._mutate(change)

    def deactivate(self) -> None:
        self._mutate(lambda document: (_document(None, document.profiles), None))

    def remove(self, name: str) -> PlatformProfile:
        """Forget a profile (credentials are the caller's decision); clears it when active."""
        profile_name(name)

        def change(document: ProfileDocument) -> tuple[ProfileDocument, PlatformProfile]:
            removed = _existing(document, name)
            profiles = {key: value for key, value in document.profiles.items() if key != removed.name}
            return _document(None if document.active == removed.name else document.active, profiles), removed

        return self._mutate(change)

    def set_workspace(self, name: str, selection: WorkspaceSelection | None) -> PlatformProfile:
        return self._update(name, workspace=selection)

    def set_account(self, name: str, hint: AccountHint | None) -> PlatformProfile:
        """Record (or clear) the last signed-in account; `WV-PROFILE-ACCOUNT` for another provider's hint."""
        return self._update(name, account=hint)

    # --- Studio preferences ------------------------------------------------------------------------------------

    def load_preferences(self) -> StudioPreferences:
        """Studio's preferences; missing means the defaults. A damaged file is reported, never repaired."""
        try:
            with self._directory(create=False) as directory:
                raw = self._read_raw(
                    directory, PREFERENCES_FILE, MAX_PREFERENCES_BYTES, "The Studio preferences file exceeds 16 KiB."
                )
        except FileNotFoundError:
            return StudioPreferences()
        except OSError:
            raise _store_error() from None
        if raw is None:
            return StudioPreferences()
        try:
            return StudioPreferences.model_validate_json(raw)
        except ValidationError:
            raise _store_error("The Studio preferences file is damaged or from a newer version.") from None

    def save_preferences(self, preferences: StudioPreferences) -> StudioPreferences:
        """Replace `studio.json` atomically (an explicit choice, so a damaged file is replaced too)."""
        try:
            with self._transaction() as directory:
                self._write_raw(directory, PREFERENCES_FILE, (preferences.model_dump_json(indent=2) + "\n").encode())
        except OSError:
            raise _store_error() from None
        return preferences

    def _update(self, name: str, **changes: Any) -> PlatformProfile:
        profile_name(name)

        def change(document: ProfileDocument) -> tuple[ProfileDocument, PlatformProfile]:
            existing = _existing(document, name)
            hint = changes.get("account")
            if isinstance(hint, AccountHint) and (
                hint.issuer != existing.login.issuer or hint.provider_id != existing.login.provider_id
            ):
                raise ProfileError("WV-PROFILE-ACCOUNT")
            stored = _updated(existing, **changes)
            return _document(document.active, {**document.profiles, existing.name: stored}), stored

        return self._mutate(change)

    def _mutate(self, change: Callable[[ProfileDocument], tuple[ProfileDocument, _Result]]) -> _Result:
        try:
            with self._transaction() as directory:
                document, result = change(self._read(directory))
                self._write(directory, document)
                return result
        except OSError:
            raise _store_error() from None

    # --- file system -------------------------------------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[int | None]:
        """Create or tighten the directory and hold the cross-process lock."""
        with self._directory(create=True) as directory:
            descriptor = self._open_private(directory, self.path.name + ".lock", os.O_RDWR | os.O_CREAT)
            try:
                deadline = time.monotonic() + LOCK_TIMEOUT
                while not _try_lock(descriptor):
                    if time.monotonic() >= deadline:
                        raise _store_error("Another process is still updating the saved platforms.")
                    time.sleep(0.02)
                try:
                    yield directory
                finally:
                    _unlock(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def _directory(self, *, create: bool) -> Iterator[int | None]:
        """POSIX: a no-follow descriptor for the store directory. Windows: None (paths are used).

        Raises FileNotFoundError when the directory is missing and `create` is false.
        """
        parent = self.path.parent
        if create:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _windows():
            if parent.is_symlink() or parent.is_junction():
                raise _store_error("The saved platforms directory must not be a link.")
            if not parent.is_dir():
                raise FileNotFoundError(str(parent))
            yield None
            return
        try:
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise
        except OSError:
            raise _store_error("The saved platforms directory must be a real directory, not a link.") from None
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid():
                raise _store_error("The saved platforms directory belongs to another user.")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_private(self, directory: int | None, name: str, flags: int) -> int:
        """Open a private regular file in the store directory, refusing links; tighten POSIX modes."""
        if directory is None:
            path = self.path.parent / name
            if path.is_symlink():
                raise _store_error("The saved platforms files must not be links.")
            descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise _store_error("The saved platforms files must be regular files.")
            return descriptor
        try:
            descriptor = _open_at(directory, name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise
        except OSError:
            raise _store_error("The saved platforms files must be private regular files, not links.") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise _store_error("The saved platforms files must be private regular files, not links.")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _read(self, directory: int | None) -> ProfileDocument:
        raw = self._read_raw(directory, self.path.name, MAX_STORE_BYTES, "The saved platforms file exceeds 1 MiB.")
        if raw is None:
            return ProfileDocument()
        try:
            return ProfileDocument.model_validate_json(raw)
        except (ValidationError, AuthError):
            # AuthError: LoginConfig reports an untrusted origin itself, outside ValidationError.
            # Never repair or overwrite: the person decides what to do with the file.
            raise _store_error("The saved platforms file is damaged or from a newer version.") from None

    def _read_raw(self, directory: int | None, name: str, limit: int, oversized: str) -> bytes | None:
        """The bounded bytes of a private file in the store directory, or None when it does not exist."""
        try:
            descriptor = self._open_private(directory, name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            return _read_bounded(descriptor, limit, oversized)
        finally:
            os.close(descriptor)

    def _write(self, directory: int | None, document: ProfileDocument) -> None:
        raw = (document.model_dump_json(indent=2) + "\n").encode()
        if len(raw) > MAX_STORE_BYTES:
            raise _store_error("The saved platforms file would exceed 1 MiB.")
        self._write_raw(directory, self.path.name, raw)

    def _write_raw(self, directory: int | None, name: str, raw: bytes) -> None:
        """Write a private file atomically: temporary file, fsync, replace (never through a link)."""
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = self._open_private(directory, temporary, flags)
        created = True
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if directory is None:
                _replace_windows(self.path.parent / temporary, self.path.parent / name)
            else:
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            created = False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if created:
                with suppress(OSError):
                    if directory is None:
                        (self.path.parent / temporary).unlink(missing_ok=True)
                    else:
                        os.unlink(temporary, dir_fd=directory)


def _open_at(directory: int, name: str, flags: int) -> int:
    if not flags & os.O_CREAT or flags & os.O_EXCL:
        return os.open(name, flags, 0o600, dir_fd=directory)
    # Concurrent plain O_CREAT opens can fail with ENOENT on macOS; create exclusively, else open.
    for _ in range(50):
        try:
            return os.open(name, flags | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            try:
                return os.open(name, flags & ~os.O_CREAT, dir_fd=directory)
            except FileNotFoundError:
                continue
    raise _store_error("The saved platforms lock file cannot be opened.")


def _replace_windows(source: Path, target: Path) -> None:
    # A lock-free reader may hold the old file open for an instant; Windows then refuses replacement.
    deadline = time.monotonic() + 1
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _existing(document: ProfileDocument, name: str) -> PlatformProfile:
    found = document.profiles.get(name)
    if found is None:
        # Names are unique ignoring letter case, so at most one profile can match.
        folded = name.casefold()
        found = next((value for key, value in document.profiles.items() if key.casefold() == folded), None)
    if found is None:
        raise ProfileError("WV-PROFILE-NOT-FOUND")
    return found


def _updated(profile: PlatformProfile, **changes: Any) -> PlatformProfile:
    # Revalidate so cross-field rules (for example the account hint issuer) always hold.
    return PlatformProfile.model_validate({**dict(profile), "updated_at": _now(), **changes})


def _document(active: str | None, profiles: dict[str, PlatformProfile]) -> ProfileDocument:
    return ProfileDocument(active=active, profiles=profiles)
