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

A purge deletes every entry it can read under the prefix, skips (and counts) what it can't read rather than stopping,
and removes the temporary copies an interrupted write left behind. A purge that didn't finish is remembered in the
folder and finished when Studio starts again, together with the keys of platforms that are no longer saved.
"""

import hashlib
import json
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
# The prefixes whose purge didn't finish; a dot-file, so it is never listed as a key.
PENDING: Final = ".pending-purges"
PLATFORM_PREFIX: Final = re.compile(r"p[0-9a-f]{16}:")


class StoreFull(Exception):
    code = "WV-STUDIO-STORE-FULL"


class StoreUnavailable(Exception):
    code = "WV-STUDIO-STORE"


@dataclass(frozen=True)
class Purge:
    """What a purge did: entries removed, and entries it couldn't read (so couldn't attribute)."""

    removed: int
    unreadable: int

    @property
    def complete(self) -> bool:
        return self.unreadable == 0


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

    def _pending(self, directory: int | None) -> list[str]:
        try:
            raw = self.folder.read(directory, PENDING, 64 * 1024)
            value = json.loads(raw or b"[]")
        except (OSError, ProfileError, ValueError):
            return []
        return sorted({item for item in value if isinstance(item, str) and item} if isinstance(value, list) else [])

    def _remember(self, directory: int | None, prefixes: Iterable[str]) -> None:
        wanted = sorted(set(prefixes))
        if wanted == self._pending(directory):
            return
        if wanted:
            self.folder.write(directory, PENDING, json.dumps(wanted).encode("ascii"))
        else:
            self.folder.remove(directory, PENDING)

    def pending(self) -> list[str]:
        """The prefixes whose purge hasn't finished."""
        try:
            with self.folder.reading() as directory:
                return self._pending(directory)
        except FileNotFoundError:
            return []
        except (OSError, ProfileError):
            raise StoreUnavailable() from None

    def purge(self, prefix: str) -> Purge:
        """Deletes every readable key with this prefix and every left-over temporary copy; remembers an
        incomplete purge so the next start finishes it. Raises StoreUnavailable when the folder can't be changed."""
        if not prefix:
            raise ValueError("A purge names a prefix")
        try:
            with self.folder.changing() as directory:
                return self._purge(directory, prefix)
        except FileNotFoundError:
            return Purge(0, 0)
        except (OSError, ProfileError):
            self._note_unfinished(prefix)
            raise StoreUnavailable() from None

    def _purge(self, directory: int | None, prefix: str) -> Purge:
        # Writes hold the same lock, so a temporary copy seen here belongs to a write that never finished.
        for name in self.folder.temporaries(directory):
            self.folder.remove(directory, name)
        unreadable: list[str] = []
        removed = 0
        for key, name in self._entries(directory, unreadable).items():
            if key.startswith(prefix) and self.folder.remove(directory, name):
                removed += 1
        result = Purge(removed, len(unreadable))
        others = [item for item in self._pending(directory) if item != prefix]
        self._remember(directory, others if result.complete else [*others, prefix])
        return result

    def _note_unfinished(self, prefix: str) -> None:
        try:
            with self.folder.changing() as directory:
                self._remember(directory, [*self._pending(directory), prefix])
        except (OSError, ProfileError):
            pass

    def finish_purges(self, saved: Iterable[str] | None) -> list[tuple[str, Purge]]:
        """At start-up: finish the purges that didn't, and forget platforms that are no longer saved (the prefixes of
        the saved ones; None when they aren't known, which only finishes the purges)."""
        keep = set(saved) if saved is not None else None
        try:
            with self.folder.changing() as directory:
                prefixes = set(self._pending(directory))
                if keep is not None:
                    for key in self._entries(directory):
                        match = PLATFORM_PREFIX.match(key)
                        if match and match.group(0) not in keep:
                            prefixes.add(match.group(0))
                return [(prefix, self._purge(directory, prefix)) for prefix in sorted(prefixes)]
        except FileNotFoundError:
            return []
        except (OSError, ProfileError):
            raise StoreUnavailable() from None
