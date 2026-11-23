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
from typing import get_args

from firefly_weave.contracts.operations import IncidentResolution
from firefly_weave.contracts.transactions import PARTICIPANT_STATUSES, TRANSACTION_MESSAGES, TRANSACTION_STATES

CORPUS = json.loads(Path("studio/tests/fixtures/transaction-corpus.json").read_text())


def test_the_corpus_matches_the_platform() -> None:
    assert tuple(CORPUS["states"]) == TRANSACTION_STATES
    assert tuple(CORPUS["stateWords"]) == TRANSACTION_STATES
    assert tuple(CORPUS["statuses"]) == PARTICIPANT_STATUSES
    assert CORPUS["codes"] == {code: text for code, text in TRANSACTION_MESSAGES.items() if code.startswith("WV-TXN-")}
    kinds = {offer["kind"] for offer in CORPUS["resolutions"].values()}
    assert kinds == set(get_args(IncidentResolution.model_fields["kind"].annotation))


def test_the_held_run_names_force_end() -> None:
    # The server refuses to cancel a held run without undoing, so its sentence names the one way out.
    assert CORPUS["codes"]["WV-TXN-RUN-OVERSIZED"].endswith("held until someone force ends the run.")
