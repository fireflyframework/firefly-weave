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

"""`weave workflow simulate` with transactions: one line per transaction, results that never arrive, resolutions."""

import json
import sys
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from transaction_support import INPUT, add, compiled, hold, submit, transaction  # noqa: E402

from firefly_weave.cli.main import cli  # noqa: E402

MOCKS = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}
UNDO_STUCK = {"node:add": {"code": "UNAVAILABLE"}, "node:add#undo": {"code": "UNAVAILABLE", "attempts": 10}}


def request(folder, *, errors=None, pending=None, unmocked=()):
    """A simulation request for the move workflow; a key with a failure or no result, or ``unmocked``, has no mock."""
    skipped = set(errors or {}) | set(pending or []) | set(unmocked)
    body = {
        "artifact": json.loads(compiled([transaction([hold(), add(), submit()])]).to_bytes()),
        "mocks": {key: value for key, value in MOCKS.items() if key not in skipped},
        "errors": errors or {},
        "input": INPUT,
        "now": "2026-11-12T00:00:00Z",
    }
    if pending is not None:
        body["pending"] = pending
    path = folder / "request.json"
    path.write_text(json.dumps(body))
    return path


def commands(folder, values):
    path = folder / "commands.json"
    path.write_text(json.dumps(values))
    return path


def simulate(*arguments):
    return CliRunner().invoke(cli, ["workflow", "simulate", *map(str, arguments)])


def test_simulate_prints_each_transaction(tmp_path):
    result = simulate(request(tmp_path, errors=UNDO_STUCK))
    assert result.exit_code == 1, result.output
    assert "Transaction move: in doubt (WV-TXN-UNDO-STUCK at add#undo:10)." in result.output


def test_simulate_takes_pending_keys(tmp_path):
    plain = request(tmp_path, unmocked=["node:submit"])
    waiting = simulate(plain, "--pending", "node:submit")
    assert waiting.exit_code == 0, waiting.output
    assert "Transaction move: deciding." in waiting.output
    once = simulate(plain, "--pending", "node:submit", "--output", "json")
    twice = simulate(plain, "--pending", "node:submit", "--pending", "node:submit", "--output", "json")
    both = simulate(request(tmp_path, pending=["node:submit"]), "--pending", "node:submit", "--output", "json")
    assert once.exit_code == twice.exit_code == both.exit_code == 0, both.output
    assert once.output == twice.output == both.output


def test_a_command_file_resolves_and_cancels(tmp_path):
    resolved = [
        {"kind": "continue"},
        {"kind": "resolve", "incident_key": "add#undo:10", "resolution": "retry_safe"},
        {"kind": "continue"},
    ]
    retried = simulate(request(tmp_path, errors=UNDO_STUCK), "--commands", commands(tmp_path, resolved))
    assert retried.exit_code == 1, retried.output
    assert "Transaction move: compensated." in retried.output
    cancelled = simulate(
        request(tmp_path), "--commands", commands(tmp_path, [{"kind": "cancel"}, {"kind": "continue"}])
    )
    assert cancelled.output.startswith("Simulation cancelled"), cancelled.output
