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


"""Studio's platform event corpus says what the platform says: its event types, events it would send, its reasons."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from firefly_weave.compiler.platform_catalog import platform_schema_bundle
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.event_triggers import EVENT_TRIGGER_MESSAGES, EventStartCode
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA
from firefly_weave.contracts.transactions import TRANSACTION_EVENT_TYPES, TRANSACTION_MESSAGES

CORPUS = json.loads(Path("studio/tests/fixtures/platform-event-corpus.json").read_text())
SAMPLES = [case["event"] for case in CORPUS["samples"]["cases"]]


def test_the_event_types_are_the_platforms_in_its_order() -> None:
    assert [entry["type"] for entry in CORPUS["events"]] == list(TRANSACTION_EVENT_TYPES)


def test_there_is_one_sample_for_each_event_type() -> None:
    assert [sample["type"] for sample in SAMPLES] == list(TRANSACTION_EVENT_TYPES)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["type"])
def test_each_sample_is_an_event_the_platform_would_send(sample: dict[str, Any]) -> None:
    # The platform's own model reads the sample and writes it back unchanged: no field missing, extra or reshaped.
    assert PlatformEvent.model_validate_json(json.dumps(sample)).model_dump(mode="json") == sample
    # A workflow whose input is the platform event's schema accepts it, as a run's start checks.
    assert validate_payload({"$ref": PLATFORM_EVENT_SCHEMA}, sample, platform_schema_bundle()) == ()


def test_each_start_sentence_is_the_platforms() -> None:
    messages = {**TRANSACTION_MESSAGES, **EVENT_TRIGGER_MESSAGES}
    assert set(CORPUS["starts"]) == set(get_args(EventStartCode.__value__))
    assert CORPUS["starts"] == {code: messages[code] for code in CORPUS["starts"]}
