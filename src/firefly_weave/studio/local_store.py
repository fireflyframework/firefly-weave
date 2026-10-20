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

"""What Studio keeps on this computer when it runs in the desktop app.

The desktop window keeps no browser storage after it quits, so Studio writes the keys it would keep in the browser
(local drafts, their test data and canvases, preferences, account-prefixed copies) through the host instead. Each key
is one private file under ``<config dir>/studio-store/``, named by the SHA-256 of the key (keys hold ``:``, which
Windows refuses in file names) and holding ``{"key", "value"}``. Values are text Studio already serialized; the host
never reads them. A saved platform's keys share its prefix, so signing out or removing it deletes them here.

Signing out of a platform or removing it writes a tombstone for its prefix in the folder, then purges: every entry
under the prefix goes, and so does every entry nobody can read or attribute (a link is unlinked, never followed; a
folder is removed without following links; an oversized file is unlinked) and every temporary copy an interrupted
write left behind. Only counts are logged, never names or values. Studio refuses writes under a tombstoned prefix
and, when it starts, purges every tombstoned prefix again; a sign-in to that platform lifts its tombstone. Keys of a
platform that is neither saved nor tombstoned (an older Studio's name, say) are left alone and only counted.
"""

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from firefly_weave.sdk.profiles import PrivateFolder, ProfileError, default_config_dir

STORE_DIRECTORY: Final = "studio-store"
KEY: Final = re.compile(r"[A-Za-z0-9:._-]{1,256}")
PREFIX: Final = re.compile(r"[A-Za-z0-9:._-]{0,256}")
STORE_LIMIT: Final = 64 * 1024 * 1024
# A value arrives in a request body, which the host bounds at 2 MiB; its file adds the key and quotes.
FILE_LIMIT: Final = 2 * 1024 * 1024 + 4096
# The prefixes of platforms signed out or removed; a dot-file, so it is never listed as a key.
TOMBSTONES: Final = ".tombstones"
PLATFORM_PREFIX: Final = re.compile(r"p[0-9a-f]{16}:")


class StoreFull(Exception):
    code = "WV-STUDIO-STORE-FULL"


class StoreUnavailable(Exception):
    code = "WV-STUDIO-STORE"


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Purge:
    """What a purge did: the prefix's entries removed, unreadable entries discarded, and entries that stayed."""

    removed: int
    discarded: int = 0
    failed: int = 0

    @property
    def complete(self) -> bool:
        return self.failed == 0


@dataclass(frozen=True)
class Startup:
    """What starting the store did: each tombstoned prefix purged again, and keys of untracked platforms counted."""

    purged: list[tuple[str, Purge]]
    untracked: int


def profile_prefix(name: str) -> str:
    """The prefix of every key a saved platform owns: "p", 16 hex digits of the name's SHA-256, and a colon."""
    return "p" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] + ":"


def file_name(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest() + ".json"


class LocalStore:
    """One private file per key; at most 64 MiB in all."""

    def __init__(self, directory: Path | None = None) -> None:
        self.folder = PrivateFolder(directory if directory is not None else default_config_dir() / STORE_DIRECTORY)

    def _entries(self, directory: int | None, unreadable: list[str] | None = None) -> dict[str, str]:
        """Key to file name, for every readable entry; the names it can't read go to ``unreadable``."""
        found: dict[str, str] = {}
        for name in self.folder.names(directory):
            try:
                raw = self.folder.read(directory, name, FILE_LIMIT)
            except (OSError, ProfileError):
                # A link, a folder, a file over the limit: never followed, never deleted, reported.
                if unreadable is not None:
                    unreadable.append(name)
                continue
            try:
                entry = json.loads(raw or b"")
            except ValueError:
                continue
            key = entry.get("key") if isinstance(entry, dict) else None
            if isinstance(key, str) and KEY.fullmatch(key) and file_name(key) == name:
                found[key] = name
        return found

    def keys(self, prefix: str = "") -> list[str]:
        try:
            with self.folder.reading() as directory:
                return sorted(key for key in self._entries(directory) if key.startswith(prefix))
        except FileNotFoundError:
            return []
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def get(self, key: str) -> str | None:
        try:
            with self.folder.reading() as directory:
                raw = self.folder.read(directory, file_name(key), FILE_LIMIT)
        except FileNotFoundError:
            return None
        except (OSError, ProfileError):
            raise StoreUnavailable() from None
        if raw is None:
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            # A damaged file: reported, never answered as a value.
            raise StoreUnavailable() from None
        if not isinstance(entry, dict):
            raise StoreUnavailable()
        return entry["value"] if entry.get("key") == key and isinstance(entry.get("value"), str) else None

    def put(self, key: str, value: str) -> None:
        try:
            raw = json.dumps({"key": key, "value": value}, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Values are text without lone surrogates") from None
        name = file_name(key)
        try:
            with self.folder.changing() as directory:
                used = sum(self.folder.size(directory, item) for item in self.folder.names(directory) if item != name)
                if used + len(raw) > STORE_LIMIT:
                    raise StoreFull()
                self.folder.write(directory, name, raw)
        except StoreFull:
            raise
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def delete(self, key: str) -> bool:
        try:
            with self.folder.changing() as directory:
                return self.folder.remove(directory, file_name(key))
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def _tombstones(self, directory: int | None) -> list[str]:
        try:
            raw = self.folder.read(directory, TOMBSTONES, 64 * 1024)
            value = json.loads(raw or b"[]")
        except (OSError, ProfileError, ValueError):
            return []
        return sorted({item for item in value if isinstance(item, str) and item} if isinstance(value, list) else [])

    def _set_tombstones(self, directory: int | None, prefixes: Iterable[str]) -> None:
        wanted = sorted(set(prefixes))
        if wanted == self._tombstones(directory):
            return
        if wanted:
            self.folder.write(directory, TOMBSTONES, json.dumps(wanted).encode("ascii"))
        else:
            self.folder.remove(directory, TOMBSTONES)

    def tombstones(self) -> list[str]:
        """The prefixes of platforms signed out or removed and not signed in since."""
        try:
            with self.folder.reading() as directory:
                return self._tombstones(directory)
        except FileNotFoundError:
            return []
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def forget(self, prefix: str) -> Purge:
        """Sign-out or removal: tombstone the prefix, then purge it. Raises StoreUnavailable when the folder can't
        be changed; the tombstone, once written, makes the next start purge again."""
        if not prefix:
            raise ValueError("A purge names a prefix")
        try:
            with self.folder.changing() as directory:
                self._set_tombstones(directory, [*self._tombstones(directory), prefix])
                return self._purge(directory, prefix)
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def remember(self, prefix: str) -> None:
        """A sign-in lifts the platform's tombstone."""
        try:
            with self.folder.changing() as directory:
                self._set_tombstones(directory, [item for item in self._tombstones(directory) if item != prefix])
        except FileNotFoundError:
            return
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def purge(self, prefix: str) -> Purge:
        """Deletes every key with this prefix, every entry nobody can read and every left-over temporary copy."""
        if not prefix:
            raise ValueError("A purge names a prefix")
        try:
            with self.folder.changing() as directory:
                return self._purge(directory, prefix)
        except FileNotFoundError:
            return Purge(0)
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def _purge(self, directory: int | None, prefix: str) -> Purge:
        failed = 0
        # Writes hold the same lock, so a temporary copy seen here belongs to a write that never finished.
        for name in self.folder.temporaries(directory):
            try:
                self.folder.remove(directory, name)
            except OSError:
                failed += 1
        unreadable: list[str] = []
        removed = discarded = 0
        for key, name in self._entries(directory, unreadable).items():
            if key.startswith(prefix):
                try:
                    removed += self.folder.remove(directory, name)
                except OSError:
                    failed += 1
        # Nobody can attribute an entry Studio can't read: in Studio's own private folder it goes, unfollowed.
        for name in unreadable:
            try:
                discarded += self.folder.discard(directory, name)
            except OSError:
                failed += 1
        if discarded or failed:
            _log.warning(
                "Studio's store discarded %d unreadable entries; %d entries could not be removed", discarded, failed
            )
        return Purge(removed, discarded, failed)

    def finish_purges(self, saved: Iterable[str] | None = None) -> Startup:
        """At start-up: purge every tombstoned prefix again, and count the keys of platforms that are neither saved
        (``saved`` holds their prefixes, when known) nor tombstoned; those are left alone."""
        try:
            with self.folder.changing() as directory:
                tombstoned = self._tombstones(directory)
                purged = [(prefix, self._purge(directory, prefix)) for prefix in tombstoned]
                known = {*tombstoned, *(saved or ())}
                untracked = 0
                for key in self._entries(directory):
                    match = PLATFORM_PREFIX.match(key)
                    if match and match.group(0) not in known and saved is not None:
                        untracked += 1
                if untracked:
                    _log.info("Studio's store holds %d keys of platforms it doesn't know; it leaves them", untracked)
                return Startup(purged, untracked)
        except FileNotFoundError:
            return Startup([], 0)
        except (OSError, ProfileError):
            raise StoreUnavailable() from None
