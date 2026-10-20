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
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Final

from firefly_weave.sdk.profiles import PrivateFolder, ProfileError, default_config_dir

STORE_DIRECTORY: Final = "studio-store"
KEY: Final = re.compile(r"[A-Za-z0-9:._-]{1,256}")
PREFIX: Final = re.compile(r"[A-Za-z0-9:._-]{0,256}")
STORE_LIMIT: Final = 64 * 1024 * 1024
# A value arrives in a request body, which the host bounds at 2 MiB; its file adds the key and quotes.
FILE_LIMIT: Final = 2 * 1024 * 1024 + 4096


class StoreFull(Exception):
    code = "WV-STUDIO-STORE-FULL"


class StoreUnavailable(Exception):
    code = "WV-STUDIO-STORE"


def profile_prefix(name: str) -> str:
    """The prefix of every key a saved platform owns: "p", 16 hex digits of the name's SHA-256, and a colon."""
    return "p" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] + ":"


def file_name(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest() + ".json"


class LocalStore:
    """One private file per key; at most 64 MiB in all."""

    def __init__(self, directory: Path | None = None) -> None:
        self.folder = PrivateFolder(directory if directory is not None else default_config_dir() / STORE_DIRECTORY)

    def _entries(self, directory: int | None) -> dict[str, str]:
        """Key to file name, for every readable entry."""
        found: dict[str, str] = {}
        for name in self.folder.names(directory):
            raw = self.folder.read(directory, name, FILE_LIMIT)
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
        entry = json.loads(raw)
        return entry["value"] if entry.get("key") == key and isinstance(entry.get("value"), str) else None

    def put(self, key: str, value: str) -> None:
        raw = json.dumps({"key": key, "value": value}, ensure_ascii=False).encode("utf-8")
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

    def purge(self, prefix: str) -> int:
        """Deletes every key with this prefix; returns how many went."""
        if not prefix:
            raise ValueError("A purge names a prefix")
        try:
            with self.folder.changing() as directory:
                removed = 0
                for key, name in self._entries(directory).items():
                    if key.startswith(prefix) and self.folder.remove(directory, name):
                        removed += 1
                return removed
        except FileNotFoundError:
            return 0
        except (OSError, ProfileError):
            raise StoreUnavailable() from None
