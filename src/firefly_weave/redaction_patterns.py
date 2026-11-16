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

"""The pattern pass: redact secret-shaped text in values that no schema marks as secret.

Schemas mark the secrets Weave knows about (``x-secret``, ``writeOnly``). This pass is the second line, for text that
another system returns in a field it never declared secret: bearer tokens, JWTs, private key blocks, well-known API key
shapes and the user information of URLs. Each match becomes ``[redacted]``.

The sets are numbered and frozen. A recorded AI agent run replays with the set its rules named, so a set never changes
once it ships: a new pattern goes in a new set. Every pattern is linear on untrusted text (bounded or anchored runs,
no nested quantifiers).
"""

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from firefly_weave.contracts.values import JsonValue

REDACTED: Final = "[redacted]"

# Set 1. A private key block runs to its END line, or to the end of the text when a cut removed it.
_SET_1: Final = (
    re.compile(r"-----BEGIN [A-Z0-9 ]{1,64}-----(?:(?!-----END )[\s\S])*(?:-----END [A-Z0-9 ]{1,64}-----)?"),
    re.compile(r"\b[Bb][Ee][Aa][Rr][Ee][Rr][ \t]+[A-Za-z0-9._~+/-]{8,}=*"),
    # A token starts where no token character comes before it; ``\b`` would let every ``-eyJ`` inside one long run
    # start a match that scans to the run's end, which is quadratic.
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
)
# URL user information keeps the scheme and the host around it: ``https://[redacted]@example.test/``. The run is
# possessive and stops at ``/``, so the runs after each ``://`` never overlap and the pass stays linear at any length.
_USER_INFO: Final = re.compile(r"(?<=://)[^\s/?#@]++(?=@)")

PATTERN_SETS: Final[Mapping[int, tuple[re.Pattern[str], ...]]] = MappingProxyType({1: (*_SET_1, _USER_INFO)})


def scrub(value: JsonValue, patterns: int) -> tuple[JsonValue, int]:
    """``value`` with every secret-shaped run of text replaced by ``[redacted]``, and how many runs were replaced.

    Object keys are scrubbed too; two keys that become the same keep both entries (``[redacted] (2)``).
    """
    try:
        compiled = PATTERN_SETS[patterns]
    except KeyError:
        raise ValueError(f"Unknown redaction pattern set {patterns}") from None
    count = 0

    def text(source: str) -> str:
        nonlocal count
        for pattern in compiled:
            source, replaced = pattern.subn(REDACTED, source)
            count += replaced
        return source

    return _walk(value, text), count


def _walk(value: JsonValue, text: Callable[[str], str]) -> JsonValue:
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [_walk(item, text) for item in value]
    if isinstance(value, dict):
        scrubbed: dict[str, JsonValue] = {}
        for key, item in value.items():
            name = text(key)
            if name in scrubbed:
                suffix = 2
                while f"{name} ({suffix})" in scrubbed or f"{name} ({suffix})" in value:
                    suffix += 1
                name = f"{name} ({suffix})"
            scrubbed[name] = _walk(item, text)
        return scrubbed
    return value
