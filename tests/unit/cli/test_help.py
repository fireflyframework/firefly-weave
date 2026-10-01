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

"""Human help stays discoverable without contaminating machine output."""

import json

import pytest
from click.testing import CliRunner

from firefly_weave import __version__
from firefly_weave.cli.main import cli


@pytest.mark.parametrize("args", [[], ["--help"], ["help"]])
def test_root_help_is_successful_and_shows_practical_entry_points(args):
    result = CliRunner().invoke(cli, args, prog_name="weave")
    assert result.exit_code == 0, result.output
    assert "Firefly Weave" in result.output
    assert "Quick start" in result.output
    assert "weave workflow validate workflow.yaml" in result.output
    assert "weave worker deploy --help" in result.output
    assert "WV-CLI-USAGE" not in result.output


def test_help_examples_follow_the_invocation_name():
    result = CliRunner().invoke(cli, ["--help"], prog_name="weave-dev")
    assert result.exit_code == 0, result.output
    assert "weave-dev workflow validate workflow.yaml" in result.output


@pytest.mark.parametrize("path", [["workflow"], ["workflow", "compile"], ["admin", "migrate"]])
def test_help_navigation_matches_direct_help_without_running_command(path):
    runner = CliRunner()
    indirect = runner.invoke(cli, ["help", *path], prog_name="weave")
    direct = runner.invoke(cli, [*path, "--help"], prog_name="weave")
    assert indirect.exit_code == direct.exit_code == 0, indirect.output
    assert indirect.output == direct.output
    assert "Quick start" not in indirect.output


@pytest.mark.parametrize("path", [["do-not-echo"], ["workflow", "do-not-echo"], ["version", "do-not-echo"]])
def test_unknown_help_path_keeps_usage_errors_value_free(path):
    result = CliRunner().invoke(cli, ["help", *path])
    assert result.exit_code == 2
    assert "WV-CLI-USAGE" in result.output
    assert "do-not-echo" not in result.output


def test_standard_version_option_has_no_banner():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output == f"Firefly Weave {__version__}\n"


def test_machine_version_remains_one_json_document():
    result = CliRunner().invoke(cli, ["version", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "version": __version__,
        "apiVersion": "weave/v1alpha1",
        "irVersion": "weave/ir-v1alpha1",
    }


def test_root_help_explains_public_command_families_without_generated_boilerplate():
    result = CliRunner().invoke(cli, ["--help"], prog_name="weave", terminal_width=80)
    assert result.exit_code == 0, result.output
    assert "Typed public" not in result.output
    for summary in (
        "Publish and activate workflows and actions.",
        "Start and inspect workflow runs.",
        "Configure and test integration connections.",
        "Inspect and retry outgoing event deliveries.",
    ):
        assert summary in result.output


@pytest.mark.parametrize("width", [32, 48, 80])
def test_help_ascii_banner_and_entry_points_fit_terminal(width):
    result = CliRunner().invoke(cli, ["--help"], prog_name="weave", terminal_width=width)
    assert result.exit_code == 0, result.output
    banner = result.output.split("Usage:")[0]
    assert "Firefly Weave" in banner
    assert banner.isascii()
    assert all(len(line) <= width for line in banner.splitlines())
    assert "weave init" in result.output
    assert "weave docs platform" in result.output
    assert "worker deploy" in result.output
    assert "Start here" in result.output


@pytest.mark.parametrize("path", [["init"], ["docs"], ["worker", "deploy"], ["workflow", "simulate"]])
def test_new_help_navigation_matches_direct_help(path):
    runner = CliRunner()
    indirect = runner.invoke(cli, ["help", *path], prog_name="weave")
    direct = runner.invoke(cli, [*path, "--help"], prog_name="weave")
    assert indirect.exit_code == direct.exit_code == 0, indirect.output
    assert indirect.output == direct.output
