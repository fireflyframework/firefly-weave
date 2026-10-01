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

"""Redacted structured errors. Provider/transport bodies never enter exception text."""

from firefly_weave.contracts.public import Problem


class WeaveError(Exception):
    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        self.status = problem.status
        self.code = problem.code
        self.request_id = problem.request_id
        self.diagnostics = tuple(problem.diagnostics)
        super().__init__(f"{self.code} (HTTP {self.status})")


class PreconditionFailed(WeaveError):
    pass


class TransportError(WeaveError):
    pass


class ContractError(WeaveError):
    pass
