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

"""Secrets out of log text, before an entry is sent, stored or shown.

The SDK runs it on every entry a worker or connector writes, and the server runs it again on what arrives. It hides:

- the credential values leased to the task, by exact match (also URL-encoded and JSON-escaped);
- the values of ``Authorization``, ``Proxy-Authorization``, ``Cookie`` and ``Set-Cookie``, to their closing quote or
  the end of the text;
- values assigned to a secret-named key (``password=...``, ``"api_key": "..."``, ``client_secret: ...``);
- URL query parameters whose name holds token, key, sig, secret, password, code or auth;
- what the shared pattern set catches: private key blocks, bearer tokens, JWTs, ``sk-``, ``AKIA``/``ASIA``,
  GitHub tokens and URL user information.

Format characters are removed first, so an invisible character can't split a secret; control characters and line
separators become spaces, so an entry is one line. A message keeps 512 characters, a field value 256, an entry 16
fields; fields whose name is secret-named are dropped. Each hidden value counts once, and text that is already
redacted counts nothing, so a second pass changes nothing. Every pattern is linear on untrusted text. It imports
only the standard library and ``firefly_weave.redaction_patterns``, so workers and the runner can use it too.
"""

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from firefly_weave.redaction_patterns import PATTERN_SETS, REDACTED

MESSAGE_LIMIT: Final = 512
FIELD_LIMIT: Final = 256
FIELD_COUNT: Final = 16
# Text longer than this is cut before redaction, so the work stays bounded; only a secret longer than what remains
# after the 512-character cut could straddle it.
INPUT_LIMIT: Final = 65536
# A leased value shorter than this can't be told apart from ordinary text, so it isn't matched.
KNOWN_MINIMUM: Final = 4
SAFE_INTEGER: Final = 2**53 - 1

# Field and key names that hold secrets: a field so named is dropped, a value assigned to one is hidden. Token
# counts (input_tokens, max_tokens) and tokenizers are not secrets.
SECRET_NAME: Final = re.compile(r"(?i)secret|token(?!s|iz)|passw(?:or)?d|authorization|cookie|api[_-]?key|credential")
# URL query parameter names that hold secrets.
SECRET_PARAMETER: Final = re.compile(r"(?i)token|key|sig|secret|passw(?:or)?d|code|auth|credential")
_FIELD_KEY: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
# Keys are found first and a value is read only after a secret-named key, so a value never swallows the key after
# it. A value runs to its closing quote (or the end of the text without one); unquoted, to the end of the text for
# headers, whose values hold spaces, and to the next delimiter for other keys. A value that is already hidden is
# taken whole first, so a second pass neither splits nor recounts it. Runs are possessive and keys start only where
# no key character comes before them, so every scan is linear.
_QUOTED: Final = r"(?:\"(?P<dq>[^\"]*+)\"?|'(?P<sq>[^']*+)'?|"
_SEPARATOR: Final = r"[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
_HEADER: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:(?:proxy-)?authorization|(?:set-)?cookie)(?=" + _SEPARATOR + ")"
)
_HEADER_VALUE: Final = re.compile(_SEPARATOR + _QUOTED + r"(?P<bare>\[redacted\]|[^\"']++))")
_KEY: Final = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{1,128}+(?=" + _SEPARATOR + ")")
_KEY_VALUE: Final = re.compile(_SEPARATOR + _QUOTED + r"(?P<bare>\[redacted\]|[^\s&,;\"'}\])]++))")
_PARAMETER: Final = re.compile(r"(?<=[?&;])[A-Za-z0-9_.%-]{1,128}+(?==)")
_PARAMETER_VALUE: Final = re.compile(r"=(?P<bare>\[redacted\]|[^&#\s\"'<>]++)")
_SHARED: Final = PATTERN_SETS[1]


@dataclass(frozen=True)
class Redacted:
    text: str
    redactions: int
    truncated: bool


@dataclass(frozen=True)
class RedactedEntry:
    message: str
    fields: dict[str, str | int | float | bool | None]
    redactions: int
    truncated: bool


class _Count:
    def __init__(self) -> None:
        self.value = 0

    def hide(self, value: str) -> str:
        """``[redacted]`` for a value, counted unless it was already hidden (whole, or cut at the end of a text)."""
        if not value:
            return value
        if not REDACTED.startswith(value):
            self.value += 1
        return REDACTED


def _value(match: re.Match[str], count: _Count) -> str:
    """The match with its value hidden: the separator and quotes stay, so the entry still reads."""
    groups = match.groupdict()
    group = next(name for name in ("dq", "sq", "bare") if groups.get(name) is not None)
    start, end = match.span(group)
    whole = match.group(0)
    offset = match.start()
    return whole[: start - offset] + count.hide(groups[group]) + whole[end - offset :]


def _keyed(
    text: str, keys: re.Pattern[str], values: re.Pattern[str], secret: re.Pattern[str] | None, count: _Count
) -> str:
    """``text`` with the value after each key that ``secret`` finds in its name (every key without one) hidden."""
    position, parts = 0, []
    for key in keys.finditer(text):
        if key.start() < position or (secret is not None and secret.search(key.group(0)) is None):
            continue
        value = values.match(text, key.end())
        if value is None:
            continue
        parts.append(text[position : key.end()])
        parts.append(_value(value, count))
        position = value.end()
    parts.append(text[position:])
    return "".join(parts)


def _known_forms(known: frozenset[str]) -> list[str]:
    forms: set[str] = set()
    for value in known:
        if len(value) < KNOWN_MINIMUM:
            continue
        forms.update({value, quote(value, safe=""), json.dumps(value, ensure_ascii=False)[1:-1]})
    # Longest first, so a value that contains another is hidden whole.
    # A form inside the marker itself would be found again in what this pass wrote.
    return sorted(
        (form for form in forms if len(form) >= KNOWN_MINIMUM and form not in REDACTED), key=len, reverse=True
    )


def _visible(text: str) -> str:
    return "".join(character for character in text if unicodedata.category(character) not in {"Cf", "Cs"})


def _spaced(text: str) -> str:
    return "".join(" " if unicodedata.category(character) in {"Cc", "Zl", "Zp"} else character for character in text)


def _hidden(text: str, known: list[str], count: _Count) -> str:
    for value in known:
        if value in text:
            count.value += text.count(value)
            text = text.replace(value, REDACTED)
    # Headers first: their values hold spaces another key's value would stop at.
    text = _keyed(text, _HEADER, _HEADER_VALUE, None, count)
    text = _keyed(text, _KEY, _KEY_VALUE, SECRET_NAME, count)
    text = _keyed(text, _PARAMETER, _PARAMETER_VALUE, SECRET_PARAMETER, count)
    for pattern in _SHARED:
        text = pattern.sub(lambda match: count.hide(match.group(0)), text)
    return text


def _redact(text: str, known: list[str], limit: int, count: _Count) -> tuple[str, bool]:
    truncated = len(text) > INPUT_LIMIT
    # Control characters become spaces before the patterns run, so a second pass reads the same words as the first.
    text = _hidden(_spaced(_visible(text[:INPUT_LIMIT])), known, count)
    if len(text) > limit:
        return text[:limit], True
    return text, truncated


def redact_text(text: str, *, known: frozenset[str] = frozenset(), limit: int = MESSAGE_LIMIT) -> Redacted:
    """``text`` without secrets, format or control characters, cut to ``limit`` characters."""
    count = _Count()
    result, truncated = _redact(text, _known_forms(known), limit, count)
    return Redacted(result, count.value, truncated)


def _field(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value if -SAFE_INTEGER <= value <= SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str, allow_nan=True)
    except (TypeError, ValueError, RecursionError):
        return str(value)


def redact_entry(message: str, fields: Mapping[str, object], *, known: frozenset[str] = frozenset()) -> RedactedEntry:
    """A log entry without secrets: the message and every field value redacted, secret-named fields dropped."""
    count = _Count()
    forms = _known_forms(known)
    text, truncated = _redact(message, forms, MESSAGE_LIMIT, count)
    kept: dict[str, str | int | float | bool | None] = {}
    for name, raw in fields.items():
        if not isinstance(name, str) or _FIELD_KEY.fullmatch(name) is None:
            truncated = True
            continue
        if SECRET_NAME.search(name) or _hidden(name, forms, _Count()) != name:
            count.value += 1
            continue
        if len(kept) == FIELD_COUNT:
            truncated = True
            continue
        value = _field(raw)
        if isinstance(value, str):
            value, cut = _redact(value, forms, FIELD_LIMIT, count)
            truncated = truncated or cut
        kept[name] = value
    return RedactedEntry(text, kept, count.value, truncated)
