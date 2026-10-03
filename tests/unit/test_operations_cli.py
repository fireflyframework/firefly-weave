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

"""Operations help and guided setup share the canonical API command families."""

from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_operations_help_explains_safe_order_and_exposes_plan_lifecycle():
    runner = CliRunner()
    result = runner.invoke(cli, ["operations", "--help"])
    assert result.exit_code == 0
    assert "Observe" in result.output and "runner" in result.output
    for name in ("targets", "deployments", "plans", "jobs", "runners"):
        result = runner.invoke(cli, ["operations", name, "--help"])
        assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["operations", "runner", "--help"])
    assert "setup" in result.output and "check" in result.output and "run" in result.output


def test_runner_check_invalid_file_emits_no_config_or_secret_contents(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"secret":"do-not-echo"}')
    result = CliRunner().invoke(cli, ["operations", "runner", "check", "--config", str(path)])
    assert result.exit_code != 0
    assert "do-not-echo" not in result.output
