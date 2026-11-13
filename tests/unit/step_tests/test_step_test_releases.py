# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""The server picks the only release that can run a step's dependency, and asks when several or none can."""

from uuid import uuid4

from firefly_weave.step_tests.admission import ReleaseChoice, ReleaseChoices, ReleasePins, release_pins

CRM = "crm-api@1.0.0"
MAIL = "weave-email@1.0.0"
FIRST, SECOND = ReleaseChoice(uuid4(), "crm-worker 2.1.0"), ReleaseChoice(uuid4(), "crm-worker 2.2.0")
MAILER = ReleaseChoice(uuid4(), "mail-worker 1.0.0")


def test_the_only_admitted_release_is_taken():
    assert release_pins({CRM: [FIRST], MAIL: [MAILER]}, {}) == ReleasePins({CRM: FIRST.id, MAIL: MAILER.id})
    assert release_pins({CRM: [FIRST]}, {CRM: None}) == ReleasePins({CRM: FIRST.id})


def test_several_releases_ask_the_person_to_choose():
    result = release_pins({CRM: [FIRST, SECOND], MAIL: [MAILER]}, {})
    assert result == ReleaseChoices({CRM: (FIRST, SECOND)})
    assert result.result() == {
        "choices": {CRM: [{"id": str(FIRST.id), "label": FIRST.label}, {"id": str(SECOND.id), "label": SECOND.label}]}
    }


def test_a_chosen_release_is_pinned():
    assert release_pins({CRM: [FIRST, SECOND]}, {CRM: SECOND.id}) == ReleasePins({CRM: SECOND.id})


def test_a_requested_release_must_be_one_of_the_admitted_ones():
    assert release_pins({CRM: [FIRST, SECOND]}, {CRM: uuid4()}) == ReleaseChoices({CRM: (FIRST, SECOND)})
    assert release_pins({CRM: [FIRST]}, {CRM: MAILER.id}) == ReleaseChoices({CRM: (FIRST,)})


def test_no_release_says_so():
    result = release_pins({CRM: [], MAIL: [MAILER]}, {})
    assert result == ReleaseChoices({CRM: ()})
    assert result.message() == "No release in this environment runs crm-api@1.0.0."


def test_a_step_with_no_dependency_needs_no_release():
    assert release_pins({}, {CRM: FIRST.id}) == ReleasePins({})
