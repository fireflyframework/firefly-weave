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

"""Scheduler configuration rejects invalid or incomplete execution settings."""

import pytest

from firefly_weave.settings import Settings


def test_invalid_scheduler_toggle_does_not_silently_disable(monkeypatch):
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://user@localhost:1234/db")
    monkeypatch.setenv("WEAVE_SCHEDULER_ENABLED", "typo")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_scheduler_url_requires_postgresql():
    with pytest.raises(ValueError):
        Settings(database_url="postgresql+asyncpg://user@localhost:1234/db", scheduler_database_url="sqlite:///tmp.db")


@pytest.mark.parametrize("raw", ['{"ordinary_bytes":0}', '{"control_bytes":4294967297}', " " * 32769])
def test_operations_policy_rejects_unlimited_or_oversized_configuration(monkeypatch, raw):
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://user@localhost:1234/db")
    monkeypatch.setenv("WEAVE_OPERATIONS_POLICY", raw)
    with pytest.raises(ValueError, match="WEAVE_OPERATIONS_POLICY"):
        Settings.from_env()


def test_operations_policy_configuration_is_explicit_and_lowerable(monkeypatch):
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://user@localhost:1234/db")
    monkeypatch.setenv("WEAVE_OPERATIONS_POLICY", '{"ordinary_bytes":1024}')
    assert Settings.from_env().operations.ordinary_bytes == 1024
