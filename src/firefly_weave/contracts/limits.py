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

"""Operator-owned positive budgets for parsing, compilation, and payload validation."""

from pydantic import BaseModel, ConfigDict, Field


class Limits(BaseModel):
    """Operator budgets; these are never accepted inside definition documents."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    max_source_bytes: int = Field(default=1_048_576, gt=0)
    max_steps: int = Field(default=1000, gt=0)
    max_depth: int = Field(default=32, gt=0)
    max_expression_nodes: int = Field(default=10_000, gt=0)
    max_document_nodes: int = Field(default=100_000, gt=0)
    max_payload_bytes: int = Field(default=1_048_576, gt=0)
    max_diagnostics: int = Field(default=100, gt=0)
