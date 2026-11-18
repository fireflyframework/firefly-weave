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

"""How long log entries are kept and how many an environment may write a day, from the environment."""

import pytest
from pydantic import ValidationError

from firefly_weave.settings import Settings

DATABASE_URL = "postgresql+asyncpg://user@localhost:1234/db"


def test_retention_is_seven_days_by_default_and_between_one_and_thirty(monkeypatch):
    assert Settings(database_url=DATABASE_URL).execution_log_retention_days == 7
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    for days in ("1", "30"):
        monkeypatch.setenv("WEAVE_EXECUTION_LOG_RETENTION_DAYS", days)
        assert Settings.from_env().execution_log_retention_days == int(days)
    for days in ("0", "31", "seven", "7.5"):
        monkeypatch.setenv("WEAVE_EXECUTION_LOG_RETENTION_DAYS", days)
        with pytest.raises(ValueError, match="WEAVE_EXECUTION_LOG_RETENTION_DAYS"):
            Settings.from_env()


def test_an_environment_writes_200000_entries_a_day_by_default_and_between_1000_and_5000000(monkeypatch):
    assert Settings(database_url=DATABASE_URL).execution_log_daily_entries == 200_000
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, execution_log_daily_entries=999)
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    for entries in ("1000", "5000000"):
        monkeypatch.setenv("WEAVE_EXECUTION_LOG_DAILY_ENTRIES", entries)
        assert Settings.from_env().execution_log_daily_entries == int(entries)
    for entries in ("999", "5000001", "many"):
        monkeypatch.setenv("WEAVE_EXECUTION_LOG_DAILY_ENTRIES", entries)
        with pytest.raises(ValueError, match="WEAVE_EXECUTION_LOG_DAILY_ENTRIES"):
            Settings.from_env()
