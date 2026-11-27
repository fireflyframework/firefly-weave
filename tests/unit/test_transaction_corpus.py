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

"""Studio's transaction corpus names exactly the platform's states, step statuses, codes and resolution kinds."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.operations import IncidentResolution
from firefly_weave.contracts.transactions import PARTICIPANT_STATUSES, TRANSACTION_MESSAGES, TRANSACTION_STATES
from firefly_weave.operations.debug.models import ScriptedError
from firefly_weave.operations.debug.transactions import scripted_effect
from firefly_weave.studio.local_contracts import ScriptedFailure

CORPUS = json.loads(Path("studio/tests/fixtures/transaction-corpus.json").read_text())
EFFECTS = json.loads(Path("studio/tests/fixtures/transaction-effect-cases.json").read_text())["cases"]


def test_the_corpus_matches_the_platform() -> None:
    assert tuple(CORPUS["states"]) == TRANSACTION_STATES
    assert tuple(CORPUS["stateWords"]) == TRANSACTION_STATES
    assert tuple(CORPUS["statuses"]) == PARTICIPANT_STATUSES
    assert CORPUS["codes"] == {code: text for code, text in TRANSACTION_MESSAGES.items() if code.startswith("WV-TXN-")}
    kinds = {offer["kind"] for offer in CORPUS["resolutions"].values()}
    assert kinds == set(get_args(IncidentResolution.model_fields["kind"].annotation))


@pytest.mark.parametrize("case", EFFECTS, ids=[case["name"] for case in EFFECTS])
def test_effect_cases_match_the_host(case: dict[str, Any]) -> None:
    failure = {"code": case["code"], "outcome": case["outcome"]}
    if case["outcome"] == "unknown":
        # Differs from the platform's failure_effect, which counts a definite code whatever the outcome: Studio always
        # writes the effect it shows, and the host refuses "nothing applied" for an outcome that is unknown.
        with pytest.raises(ValidationError):
            ScriptedFailure.model_validate({**failure, "effect": "none"})
        assert case["effect"] == "unknown"
    else:
        # A called workflow declares no definite failure codes.
        action = {"definiteFailures": case["definiteFailures"]} if case["kind"] == "action" else None
        assert scripted_effect(action, ScriptedError.model_validate(failure)) == case["effect"]


def test_the_held_run_names_force_end() -> None:
    # The server refuses to cancel a held run without undoing, so its sentence names the one way out.
    assert CORPUS["codes"]["WV-TXN-RUN-OVERSIZED"].endswith("held until someone force ends the run.")
