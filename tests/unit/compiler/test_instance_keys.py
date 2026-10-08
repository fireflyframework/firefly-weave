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

"""The frozen instance-key grammar (language spec 3.2, overview C11)."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from firefly_weave.contracts.instance_keys import (
    INSTANCE_KEY_PATTERN,
    InstanceKey,
    InstanceKeyText,
    InstanceKeyTextOrEmpty,
    InstanceView,
    InvalidInstanceKey,
    format_instance,
    instance_view,
    node_of,
    split_instance,
)
from firefly_weave.contracts.values import MAX_SAFE_INTEGER

# The cases of the shared fixture studio/tests/fixtures/language/instance-keys.json (scripts/language_fixtures.py),
# plus keys a JSON fixture cannot carry (a 5000-digit index and a lone surrogate).
VALID_KEYS = [
    ("send", InstanceKey("send")),
    ("send[3]", InstanceKey("send", (3,))),
    ("send[3][0]", InstanceKey("send", (3, 0))),
    ("support#2", InstanceKey("support", (), ("2",))),
    ("support#2.1", InstanceKey("support", (), ("2", "1"))),
    ("support#2.1.review", InstanceKey("support", (), ("2", "1", "review"))),
    ("support[3]#2.1.review", InstanceKey("support", (3,), ("2", "1", "review"))),
    ("notify~2", InstanceKey("notify", (), (), 2)),
    ("notify[1]~2", InstanceKey("notify", (1,), (), 2)),
    ("@join:notify", InstanceKey("@join:notify")),
    ("@branch:notify:0[4]", InstanceKey("@branch:notify:0", (4,))),
    ("a.b_c-d[9007199254740991]", InstanceKey("a.b_c-d", (MAX_SAFE_INTEGER,))),
]
INVALID_KEYS = [
    "",
    "[3]",
    "#2",
    "~1",
    "send[03]",
    "send[-1]",
    "send[]",
    "send[3",
    "send]3[",
    "send[3]x",
    "send#",
    "send#2.",
    "send#.2",
    "send#2..1",
    "send#ü",
    "send#2[3]",
    "send~0",
    "send~01",
    "send~1[2]",
    "send~1#2",
    "send~1~2",
    "send[9007199254740992]",
    "send~9007199254740992",
    "send[" + "9" * 5000 + "]",
    "a b",
    "send\n",
    "send\x00",
    "se/nd",
    "ü[1]",
    "send\ud800",
    "@",
]
TEXT = TypeAdapter(InstanceKeyText)
TEXT_OR_EMPTY = TypeAdapter(InstanceKeyTextOrEmpty)


def accepts(validate, key) -> bool:
    try:
        validate(key)
    except (InvalidInstanceKey, ValidationError):
        return False
    return True


@pytest.mark.parametrize(("key", "parsed"), VALID_KEYS)
def test_every_grammar_form_parses_and_formats_back(key, parsed):
    assert split_instance(key) == parsed
    assert format_instance(parsed) == key
    assert node_of(key) == parsed.node_id


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_non_canonical_keys_are_rejected(key):
    with pytest.raises(InvalidInstanceKey):
        split_instance(key)


@pytest.mark.parametrize(
    ("key", "valid"), [(key, True) for key, _ in VALID_KEYS] + [(key, False) for key in INVALID_KEYS]
)
def test_the_pattern_and_api_types_accept_exactly_what_split_instance_accepts(key, valid):
    assert accepts(split_instance, key) is valid
    assert (re.fullmatch(INSTANCE_KEY_PATTERN, key) is not None) is valid
    assert accepts(TEXT.validate_python, key) is valid
    assert accepts(TEXT_OR_EMPTY.validate_python, key) is (valid or key == "")
    if valid:
        assert node_of(key) == split_instance(key).node_id


def test_the_anchored_form_is_for_ecmascript_and_pydantic_not_python_re():
    key = "send[1]\n"
    # Python's "$" also matches before a trailing newline, so the anchored form is not a safe Python guard ...
    assert re.match(rf"^(?:{INSTANCE_KEY_PATTERN})$", key) is not None
    # ... while re.fullmatch on the unanchored pattern, split_instance and pydantic's default (Rust) engine reject it.
    assert re.fullmatch(INSTANCE_KEY_PATTERN, key) is None
    assert not accepts(split_instance, key)
    assert not accepts(TEXT.validate_python, key)
    assert not accepts(TEXT_OR_EMPTY.validate_python, key)


def test_the_pattern_bounds_indexes_and_counts_at_max_safe_integer():
    digits = str(MAX_SAFE_INTEGER)
    numbers = {0, 1, 9, 10, 10**15 - 1, 10**15, 10**16 - 1, 10**16, MAX_SAFE_INTEGER + 1}
    numbers |= {int(digits[:at] + digit + digits[at + 1 :]) for at in range(len(digits)) for digit in "0123456789"}
    for number in sorted(numbers):
        assert (re.fullmatch(INSTANCE_KEY_PATTERN, f"send[{number}]") is not None) is (number <= MAX_SAFE_INTEGER)
        assert (re.fullmatch(INSTANCE_KEY_PATTERN, f"send~{number}") is not None) is (1 <= number <= MAX_SAFE_INTEGER)


def test_api_types_cap_keys_at_512_characters_and_publish_the_pattern():
    longest = "s" * 509 + "[1]"
    assert TEXT.validate_python(longest) == longest
    with pytest.raises(ValidationError):
        TEXT.validate_python("s" + longest)
    assert TEXT_OR_EMPTY.validate_python("") == ""
    assert TEXT.json_schema() == {"type": "string", "maxLength": 512, "pattern": f"^(?:{INSTANCE_KEY_PATTERN})$"}
    assert TEXT_OR_EMPTY.json_schema() == {
        "type": "string",
        "maxLength": 512,
        "pattern": f"^(?:{INSTANCE_KEY_PATTERN})?$",
    }


@pytest.mark.parametrize(
    "parts",
    [
        {"node_id": ""},
        {"node_id": "a[1]"},
        {"node_id": "a#b"},
        {"node_id": "a~1"},
        {"node_id": "a]"},
        {"node_id": "a b"},
        {"node_id": "ü"},
        {"node_id": "a", "indexes": (-1,)},
        {"node_id": "a", "indexes": (True,)},
        {"node_id": "a", "indexes": (MAX_SAFE_INTEGER + 1,)},
        {"node_id": "a", "indexes": [1]},
        {"node_id": "a", "segments": ("",)},
        {"node_id": "a", "segments": ("2.1",)},
        {"node_id": "a", "count": 0},
        {"node_id": "a", "count": True},
    ],
)
def test_parts_are_validated_when_constructed(parts):
    with pytest.raises(InvalidInstanceKey):
        InstanceKey(**parts)


def test_long_keys_parse_in_linear_time():
    key = "send" + "[1]" * 20_000 + "#" + ".".join(["a"] * 20_000) + "~7"
    parsed = split_instance(key)
    assert parsed.indexes == (1,) * 20_000 and len(parsed.segments) == 20_000 and parsed.count == 7
    assert format_instance(parsed) == key


@pytest.mark.parametrize("key", ["", "[3]", "#2", "~1"])
def test_node_of_rejects_keys_without_a_node_id(key):
    with pytest.raises(InvalidInstanceKey):
        node_of(key)


def test_api_view_uses_static_node_id_and_empty_key_outside_loops_and_agents():
    assert instance_view("send") == InstanceView("send", "", [])
    assert instance_view("send[3][0]") == InstanceView("send", "send[3][0]", [3, 0])
    assert instance_view("support[2]#3.1") == InstanceView("support", "support[2]#3.1", [2])
    with pytest.raises(InvalidInstanceKey):
        instance_view("send[03]")


node_ids = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,20}|@join:[a-z]{1,5}|@branch:[a-z]{1,5}:[0-9]", fullmatch=True)
keys = st.builds(
    InstanceKey,
    node_ids,
    st.lists(st.integers(0, MAX_SAFE_INTEGER), max_size=4).map(tuple),
    st.lists(st.from_regex(r"[A-Za-z0-9]{1,6}", fullmatch=True), max_size=4).map(tuple),
    st.none() | st.integers(1, MAX_SAFE_INTEGER),
)


@given(keys)
def test_split_and_format_round_trip_for_every_form(key):
    text = format_instance(key)
    assert split_instance(text) == key
    assert format_instance(split_instance(text)) == text
    assert re.fullmatch(INSTANCE_KEY_PATTERN, text)


@given(keys)
def test_node_of_returns_the_static_id_without_separators(key):
    node = node_of(format_instance(key))
    assert node == key.node_id == split_instance(format_instance(key)).node_id
    assert not {"[", "#", "~"} & set(node)


@given(st.text(alphabet="sa9810[]#~.ü", max_size=16))
def test_the_pattern_agrees_with_split_instance_on_any_text(text):
    assert (re.fullmatch(INSTANCE_KEY_PATTERN, text) is not None) is accepts(split_instance, text)
