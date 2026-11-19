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

"""The platform accepts, refuses and fires the schedule calendars in Studio's corpus as Studio's builder does."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from firefly_weave.contracts.schedules import validate_calendar
from firefly_weave.triggers.schedules import calendar

CORPUS = json.loads(Path("studio/tests/fixtures/schedule-corpus.json").read_text())


@pytest.mark.parametrize("case", CORPUS["accepted"], ids=lambda case: repr(case["expression"]))
def test_accepted_calendars_fire_at_the_corpus_times(case: dict[str, object]) -> None:
    expression = str(case["expression"])
    after = datetime.fromisoformat(str(case.get("from", CORPUS["from"])))
    fired = calendar(expression).next_n_fire_times(5, after=after)
    assert [moment.strftime("%Y-%m-%dT%H:%M:%SZ") for moment in fired] == case["next"]


@pytest.mark.parametrize("expression", CORPUS["refused"], ids=repr)
def test_refused_calendars_are_refused(expression: str) -> None:
    with pytest.raises(ValueError):
        validate_calendar(expression)
