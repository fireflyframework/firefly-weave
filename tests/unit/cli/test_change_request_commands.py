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


"""weave operations: the typed change request commands."""

from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_change_requests_have_their_own_command_family():
    result = CliRunner().invoke(cli, ["operations", "change-requests", "--help"])
    assert result.exit_code == 0
    for command in ("create", "list", "read", "advance", "approve", "drain", "apply", "replan", "decline", "cancel"):
        assert f"  {command} " in result.output, command
    advance = CliRunner().invoke(cli, ["operations", "change-requests", "advance", "--help"])
    assert "--revision" in advance.output
