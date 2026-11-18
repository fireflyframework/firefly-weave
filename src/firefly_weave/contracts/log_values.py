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

"""The values a run log entry holds, shared by the run views and the worker protocol that writes entries."""

from typing import Annotated, Literal

from pydantic import Field

from firefly_weave.contracts.values import FiniteFloat, SafeInteger

type LogSource = Literal["engine", "worker", "connector", "ai"]
type LogLevel = Literal["debug", "info", "warning", "error"]
type LogCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
type LogFieldKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")]
type LogFieldValue = Annotated[str, Field(max_length=256)] | SafeInteger | FiniteFloat | bool | None

# Days entries are kept, and entries an environment accepts per UTC day: (default, lowest, highest).
EXECUTION_LOG_RETENTION_DAYS = (7, 1, 30)
EXECUTION_LOG_DAILY_ENTRIES = (200_000, 1_000, 5_000_000)
