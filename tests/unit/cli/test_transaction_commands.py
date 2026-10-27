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

"""CLI commands for transactions: a run's transactions, and retiring an activation by force."""

from firefly_weave.cli.remote import command, family


def test_runs_lists_its_transactions():
    assert "transactions" in family("runs", "runs").commands


def test_retire_takes_force_only_when_asked():
    retire = command("activations.retire")
    arguments = ["00000000-0000-0000-0000-000000000001", "--revision", "2", "--idempotency-key", "k"]
    arguments += ["--request", "pyproject.toml"]
    assert retire.make_context("retire", list(arguments)).params["force"] is None
    assert retire.make_context("retire", [*arguments, "--force"]).params["force"] is True
    assert "--force" not in [option for parameter in command("activations.read").params for option in parameter.opts]
