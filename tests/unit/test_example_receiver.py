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

"""The demonstration effect target retains stable idempotency receipts independently."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_receiver_replay_is_durable_and_conflicting_payload_is_rejected(tmp_path):
    spec = importlib.util.spec_from_file_location("receiver", ROOT / "examples/idempotent_receiver.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "receipts.sqlite"
    first = module.accept(path, "stable-operation", {"customer": "demo"})
    assert first == {"receipt": "accepted", "customer": "demo"}
    assert module.accept(path, "stable-operation", {"customer": "demo"}) == first
    with pytest.raises(ValueError):
        module.accept(path, "stable-operation", {"customer": "changed"})
    import sqlite3

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
