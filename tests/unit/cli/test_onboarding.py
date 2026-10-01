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

"""Offline starter generation and opt-in documentation discovery."""

import json
import socket
import webbrowser

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_starter_compiles_and_simulates_without_external_effects(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External side effect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    runner = CliRunner()
    directory = tmp_path / "starter"
    result = runner.invoke(cli, ["init", str(directory), "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert {p.name for p in directory.iterdir()} == {
        "workflow.yaml",
        "catalog.lock.json",
        "input.json",
        "simulation.json",
        "README.md",
    }
    source, catalog = directory / "workflow.yaml", directory / "catalog.lock.json"
    for command in ("validate", "compile"):
        result = runner.invoke(
            cli, ["workflow", command, str(source), "--catalog", str(catalog), "--strict", "--output", "json"]
        )
        assert result.exit_code == 0, result.output
        compilation = json.loads(result.output)
        assert compilation["ok"] is True
    request = json.loads((directory / "simulation.json").read_text())
    assert request["artifact"]["digest"] == compilation["artifact"]["digest"]
    assert request["artifact"]["executable"] == compilation["artifact"]["executable"]
    assert request["input"] == json.loads((directory / "input.json").read_text())
    result = runner.invoke(cli, ["workflow", "simulate", str(directory / "simulation.json"), "--output", "json"])
    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert view["status"] == "succeeded"
    assert view["variables"]["output"] == {"message": "Hello from Firefly Weave!"}


@pytest.mark.parametrize("obstacle", ["file", "directory-link", "file-link", "late-conflict"])
def test_starter_collision_refuses_all_writes_and_keeps_error_value_free(tmp_path, obstacle):
    directory = tmp_path / "do-not-echo"
    target = tmp_path / "untouched"
    target.write_text("original")
    if obstacle == "file":
        directory.write_text("original")
    elif obstacle == "directory-link":
        directory.symlink_to(tmp_path, target_is_directory=True)
    else:
        directory.mkdir()
        if obstacle == "file-link":
            (directory / "input.json").symlink_to(target)
        else:
            (directory / "README.md").write_text("original")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = CliRunner().invoke(cli, ["init", str(directory), "--output", "json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-INIT"
    assert "do-not-echo" not in result.output
    assert target.read_text() == "original"
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def test_starter_preserves_unrelated_files(tmp_path):
    (tmp_path / "notes.txt").write_text("keep")
    result = CliRunner().invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "notes.txt").read_text() == "keep"
    assert "workflow validate" in result.output
    assert "workflow simulate" in result.output


@pytest.mark.parametrize(
    "topic,path",
    [
        (None, ""),
        ("quickstart", "quickstart/"),
        ("platform", "guides/platform-overview/"),
        ("cli", "reference/cli/"),
        ("workers", "guides/workers/"),
        ("deploy", "operations/deployment/"),
        ("configuration", "operations/configuration/"),
    ],
)
def test_docs_prints_canonical_url_without_opening_browser(topic, path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Browser must be opt-in")

    monkeypatch.setattr(webbrowser, "open", forbidden)
    result = CliRunner().invoke(cli, ["docs", *([topic] if topic else [])])
    assert result.exit_code == 0, result.output
    assert result.output == "https://fireflyframework.github.io/firefly-weave/" + path + "\n"


def test_docs_explicit_open_uses_selected_topic(monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    result = CliRunner().invoke(cli, ["docs", "workers", "--open", "--output", "json"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["url"] == "https://fireflyframework.github.io/firefly-weave/guides/workers/"
    assert opened == [value["url"]]


def test_docs_browser_failure_is_safe_machine_error(monkeypatch):
    def failed(url):
        raise webbrowser.Error("do-not-echo")

    monkeypatch.setattr(webbrowser, "open", failed)
    result = CliRunner().invoke(cli, ["docs", "--open", "--output", "json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-DOCS"
    assert "do-not-echo" not in result.output


@pytest.mark.parametrize("args", [["init"], ["docs", "do-not-echo"]])
def test_onboarding_usage_errors_are_value_free_json(args):
    result = CliRunner().invoke(cli, [*args, "--output", "json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-USAGE"
    assert "do-not-echo" not in result.output


def test_starter_readme_refreshes_simulation_after_input_edit(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    (tmp_path / "input.json").write_text('{"message":"Edited input"}')
    result = runner.invoke(
        cli,
        [
            "workflow",
            "compile",
            str(tmp_path / "workflow.yaml"),
            "--catalog",
            str(tmp_path / "catalog.lock.json"),
            "--directory",
            str(tmp_path / "build"),
        ],
    )
    assert result.exit_code == 0, result.output
    readme = (tmp_path / "README.md").read_text()
    refresh = readme.split("python3 - <<'PYTHON'\n")[1].split("\nPYTHON")[0]
    import subprocess
    import sys

    process = subprocess.run([sys.executable, "-c", refresh], cwd=tmp_path, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    result = runner.invoke(cli, ["workflow", "simulate", str(tmp_path / "simulation.json"), "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["variables"]["output"] == {"message": "Edited input"}


def test_lumi_help_keeps_narrow_and_machine_outputs_clean():
    runner = CliRunner()
    wide = runner.invoke(cli, ["--help"], terminal_width=80)
    assert wide.exit_code == 0
    assert "Lumi, your guide" in wide.output
    assert "(o o)" in wide.output
    narrow = runner.invoke(cli, ["--help"], terminal_width=50)
    assert "Lumi" not in narrow.output
    assert "Firefly Weave" in narrow.output
    version = runner.invoke(cli, ["version", "--output", "json"])
    assert version.exit_code == 0
    assert isinstance(json.loads(version.output), dict)
    assert "Lumi" not in version.output
