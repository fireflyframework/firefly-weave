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

The SDK runs it on every entry a worker or connector writes, and the server runs it again on what arrives, by
pattern only: the server never holds the values a task leased, so only the worker can hide those. It hides:

- the credential values leased to the task, by exact match (also URL-encoded and JSON-escaped), in the worker;
- the values of ``Authorization``, ``Proxy-Authorization``, ``Cookie`` and ``Set-Cookie``, to their closing quote or
  the end of the text;
- values assigned to a secret-named key (``password=...``, ``"api_key": "..."``, ``client_secret: ...``), also in
  JSON written inside a JSON string, where the quotes are escaped;
- URL query parameters whose name holds token, key, sig, secret, password, code or auth;
- what the shared pattern set catches: private key blocks, bearer tokens, JWTs, ``sk-``, ``AKIA``/``ASIA``,
  GitHub tokens and URL user information; and Stripe keys, Slack tokens and Google API keys.

Format characters are removed first, so an invisible character can't split a secret; control characters, line
separators and other spaces become plain spaces, so an entry is one line. Leased values are cleaned the same way
before they are matched. A message keeps 512 characters, a field value 256, an entry 16 fields in 8 KiB as stored.
Fields whose name is secret-named are dropped, and so are such keys inside a nested field value, whose strings are
redacted before it is written as JSON text. Each hidden value counts once, and text that is already redacted counts
nothing, so a second pass changes nothing. Every pattern is linear on untrusted text. Besides the standard library
it imports only ``firefly_weave.redaction_patterns``, which needs pydantic for its value types and nothing of the
server, so workers and the runner can use it too.
"""

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
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
# A field value nested deeper than this is left out.
NESTING_LIMIT: Final = 32
# The fields of one entry as PostgreSQL writes them, {"key": value, ...}, hold at most this many bytes.
FIELDS_BYTES: Final = 8192

# Field and key names that hold secrets: a field so named is dropped, a value assigned to one is hidden. Token
# counts (input_tokens, max_tokens) and tokenizers are not secrets. Short names count only as a whole part of the
# name, between its ends and _ . or -, so author, passenger and keyboard stay; key counts only as the whole name.
SECRET_NAME: Final = re.compile(
    r"(?i)secret|token(?!s|iz)|passw(?:or)?d|authorization|cookie|api[_-]?key|credential"
    r"|(?:^|(?<=[_.-]))(?:pwd|pass|auth|signature|session[_-]?id|(?:private|access|signing|subscription)[_.-]?key)"
    r"(?=$|[_.-])|^key$"
)
# URL query parameter names that hold secrets.
SECRET_PARAMETER: Final = re.compile(r"(?i)token|key|sig|secret|passw(?:or)?d|code|auth|credential")
_FIELD_KEY: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
# Keys are found first and a value is read only after a secret-named key, so a value never swallows the key after
# it. A value runs to its closing quote (or the end of the text without one), past backslash-escaped quotes; in
# JSON written inside a JSON string, its quotes are escaped too (\" once, \\\" twice), and the value runs to the
# quote escaped as its opening one was. Unquoted, it runs to the end of the text for headers, whose values hold
# spaces, and to the next delimiter for other keys. A value that is already hidden is taken whole first, so a second
# pass neither splits nor recounts it. Runs are possessive, alternatives start with different characters, and keys
# start only where no key character comes before them, so every scan is linear.
_QUOTED: Final = (
    r"(?:\"(?P<dq>(?:[^\"\\]++|\\.)*+)\"?|'(?P<sq>(?:[^'\\]++|\\.)*+)'?"
    r"|(?P<escape>\\{1,7}+)\"(?P<eq>(?:[^\"\\]++|\\++(?!\")|(?!(?P=escape)\")\\++\")*+)(?:(?P=escape)\")?|"
)
_SEPARATOR: Final = r"(?:\\{0,7}+[\"'])?[ \t]{0,8}[:=][ \t]{0,8}"
# A header written as a pair, ('Cookie', '...') or ["Authorization", "..."]: its quoted name, then a comma.
_PAIR: Final = r"\\{0,7}+[\"'][ \t]{0,8},[ \t]{0,8}"
_HEADER: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:(?:proxy-)?authorization|(?:set-)?cookie)(?=" + _SEPARATOR + "|" + _PAIR + ")"
)
_HEADER_VALUE: Final = re.compile(
    "(?:" + _SEPARATOR + "|" + _PAIR + ")" + _QUOTED + r"(?P<bare>\[redacted\]|[^\"']++))"
)
_KEY: Final = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{1,128}+(?=" + _SEPARATOR + ")")
_KEY_VALUE: Final = re.compile(_SEPARATOR + _QUOTED + r"(?P<bare>\[redacted\]|[^\s&,;\"'}\])]++))")
_PARAMETER: Final = re.compile(r"(?<=[?&;])[A-Za-z0-9_.%-]{1,128}+(?==)")
_PARAMETER_VALUE: Final = re.compile(r"=(?P<bare>\[redacted\]|[^&#\s\"'<>]++)")
_SHARED: Final = PATTERN_SETS[1]
# Key shapes logs hide besides the shared set, which stays as it is for what it already protects: Stripe secret and
# restricted keys, Slack tokens, Google API keys.
_SHAPES: Final = (
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}+"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}+"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
)


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
    group = next(name for name in ("dq", "sq", "eq", "bare") if groups.get(name) is not None)
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
        # Each form as it reads once the text is cleaned: a newline or tab in the value is a space there too.
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        forms.update({_cleaned(value), quote(value, safe=""), _cleaned(escaped)})
    # Longest first, so a value that contains another is hidden whole.
    # A form inside the marker itself would be found again in what this pass wrote.
    return sorted(
        (form for form in forms if len(form) >= KNOWN_MINIMUM and form not in REDACTED), key=len, reverse=True
    )


def _visible(text: str) -> str:
    return "".join(character for character in text if unicodedata.category(character) not in {"Cf", "Cs"})


def _spaced(text: str) -> str:
    # Every space separator becomes a plain space too, so a no-break space can't keep "Bearer" from its token.
    return "".join(
        " " if unicodedata.category(character) in {"Cc", "Zl", "Zp", "Zs"} else character for character in text
    )


def _cleaned(text: str) -> str:
    return _spaced(_visible(text))


def _hidden(text: str, known: list[str], count: _Count) -> str:
    for value in known:
        if value in text:
            count.value += text.count(value)
            text = text.replace(value, REDACTED)
    # Headers first: their values hold spaces another key's value would stop at.
    text = _keyed(text, _HEADER, _HEADER_VALUE, None, count)
    text = _keyed(text, _KEY, _KEY_VALUE, SECRET_NAME, count)
    text = _keyed(text, _PARAMETER, _PARAMETER_VALUE, SECRET_PARAMETER, count)
    for pattern in (*_SHARED, *_SHAPES):
        text = pattern.sub(lambda match: count.hide(match.group(0)), text)
    return text


def _cut(text: str, known: list[str], count: _Count) -> str:
    """``text`` cut to ``INPUT_LIMIT`` characters; a leased value the cut would split is hidden whole instead, since
    hidden values before it can shrink the text enough to bring its first part into what is kept."""
    if len(text) <= INPUT_LIMIT:
        return text
    cut = INPUT_LIMIT
    for form in known:
        # Only an occurrence that starts before the limit and ends after it fits this range.
        start = text.find(form, max(0, INPUT_LIMIT - len(form) + 1), INPUT_LIMIT + len(form) - 1)
        if start != -1:
            cut = min(cut, start)
    if cut == INPUT_LIMIT:
        return text[:INPUT_LIMIT]
    count.value += 1
    return text[:cut] + REDACTED


def _redact(text: str, known: list[str], limit: int, count: _Count) -> tuple[str, bool]:
    truncated = len(text) > INPUT_LIMIT
    # Read past the limit by the longest leased form (they are sorted longest first) to see one the cut would split.
    window = text[: INPUT_LIMIT + (len(known[0]) if known else 0)]
    # Control characters become spaces before the patterns run, so a second pass reads the same words as the first.
    text = _hidden(_cut(_cleaned(window), known, count), known, count)
    if len(text) > limit:
        return text[:limit], True
    return text, truncated


def redact_text(text: str, *, known: frozenset[str] = frozenset(), limit: int = MESSAGE_LIMIT) -> Redacted:
    """``text`` without secrets, format or control characters, cut to ``limit`` characters."""
    count = _Count()
    result, truncated = _redact(text, _known_forms(known), limit, count)
    return Redacted(result, count.value, truncated)


class _Walk:
    """A nested field value made safe to write as JSON: secret-named keys dropped and strings redacted, before the
    JSON text is redacted again. The walk visits at most ``FIELD_LIMIT`` values ``NESTING_LIMIT`` levels deep, more
    than a 256-character field can show, and says when it left something out."""

    def __init__(self, forms: list[str], count: _Count) -> None:
        self.forms = forms
        self.count = count
        self.left = FIELD_LIMIT
        self.cut = False

    def value(self, value: object, depth: int) -> object:
        self.left -= 1
        if isinstance(value, str):
            text, cut = _redact(value, self.forms, FIELD_LIMIT, self.count)
            self.cut = self.cut or cut
            return text
        if not isinstance(value, Mapping | list | tuple | set | frozenset):
            return value
        if depth >= NESTING_LIMIT:
            self.cut = True
            return None
        if isinstance(value, Mapping):
            kept: dict[str, object] = {}
            for key, item in value.items():
                if self.left <= 0:
                    self.cut = True
                    break
                name = str(key)
                if SECRET_NAME.search(name) or _hidden(name, self.forms, _Count()) != name:
                    self.count.value += 1
                    continue
                kept[name] = self.value(item, depth + 1)
            return kept
        items: list[object] = []
        for item in value:
            if self.left <= 0:
                self.cut = True
                break
            items.append(self.value(item, depth + 1))
        return items


def _field(value: object, forms: list[str], count: _Count) -> tuple[str | int | float | bool | None, bool]:
    """A field value as stored, and whether part of it was cut."""
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        if -SAFE_INTEGER <= value <= SAFE_INTEGER:
            return value, False
        return str(value), False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (str(value), False)
    walk = _Walk(forms, count)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(walk.value(value, 0), ensure_ascii=False, default=str, allow_nan=True)
        except (TypeError, ValueError, RecursionError):
            text = str(value)
    redacted, cut = _redact(text, forms, FIELD_LIMIT, count)
    return redacted, cut or walk.cut


def _stored_bytes(name: str, value: str | int | float | bool | None) -> int:
    """The bytes ``"name": value`` takes where PostgreSQL writes JSON: strings escaped as JSON escapes them, and
    numbers in full, without an exponent (1e300 is 301 digits)."""
    written = format(Decimal(repr(value)), "f") if isinstance(value, float) else json.dumps(value, ensure_ascii=False)
    return len(json.dumps(name, ensure_ascii=False).encode()) + 2 + len(written.encode())


def redact_entry(message: str, fields: Mapping[str, object], *, known: frozenset[str] = frozenset()) -> RedactedEntry:
    """A log entry without secrets: the message and every field value redacted, secret-named fields dropped, and
    fields that would take the entry past ``FIELDS_BYTES`` left out."""
    count = _Count()
    forms = _known_forms(known)
    text, truncated = _redact(message, forms, MESSAGE_LIMIT, count)
    kept: dict[str, str | int | float | bool | None] = {}
    size = 2  # the braces
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
        value, cut = _field(raw, forms, count)
        added = _stored_bytes(name, value) + (2 if kept else 0)  # ", " between fields
        if size + added > FIELDS_BYTES:
            truncated = True
            continue
        size += added
        truncated = truncated or cut
        kept[name] = value
    return RedactedEntry(text, kept, count.value, truncated)
