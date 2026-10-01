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

"""Offline transport, exit status, safe exports and canonical result contracts."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.parser import parse_source

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tests/fixtures/definitions/valid/onboarding.workflow.yaml"
LOCK = ROOT / "tests/fixtures/catalog/onboarding.lock.json"


def invoke(*args):
    cli = importlib.import_module("firefly_weave.cli.main").cli
    return CliRunner().invoke(cli, list(map(str, args)))


def test_invalid_source_has_machine_diagnostic(tmp_path):
    source = tmp_path / "bad.yaml"
    source.write_text("kind: Workflow\nkind: Action\n")
    result = invoke("workflow", "validate", source, "--output", "json")
    assert result.exit_code == 1
    assert "WV-PARSE-DUPLICATE_KEY" in result.output
    assert json.loads(result.output)["errorCount"] > 0


@pytest.mark.parametrize("command,code", [("validate", 0), ("compile", 1), ("explain", 1)])
def test_no_catalog_is_explicit_partial_without_artifact(command, code):
    result = invoke("workflow", command, SOURCE, "--output", "json", "--strict")
    assert result.exit_code == code, result.output
    value = json.loads(result.output)
    assert value["partial"] and value["validationOk"] and not value["ok"]
    assert value["artifact"] is None
    text = invoke("workflow", command, SOURCE)
    assert "Partial" in text.output and "no executable" in text.output


@pytest.mark.parametrize("command", ["validate", "compile", "explain"])
def test_all_machine_commands_serialize_public_result(command, catalog):
    result = invoke("workflow", command, SOURCE, "--catalog", LOCK, "--output", "json")
    expected = compile_source(SOURCE.read_bytes(), format="yaml", filename=str(SOURCE), catalog=catalog)
    assert result.exit_code == 0, result.output
    assert result.output.strip().encode() == expected.to_bytes()
    assert import_artifact(json.loads(result.output)["artifact"]).digest == expected.artifact.digest


@pytest.mark.parametrize("problem", ["source", "catalog", "directory_source", "malformed", "shape", "digest"])
def test_local_errors_are_json_usage_errors(tmp_path, problem):
    source, lock = SOURCE, LOCK
    if problem == "source":
        source = tmp_path / "absent.yaml"
    elif problem == "directory_source":
        source = tmp_path
    elif problem == "catalog":
        lock = tmp_path / "absent.json"
    else:
        lock = tmp_path / "bad.json"
        if problem == "malformed":
            lock.write_text('{"credential":"do-not-echo-this", broken')
        elif problem == "shape":
            lock.write_text('{"definitions":[],"tasks":[null],"adapters":[],"schemas":{}}')
        else:
            value = json.loads(LOCK.read_text())
            value["definitions"][0]["digest"] = "0" * 64
            lock.write_text(json.dumps(value))
    result = invoke("workflow", "compile", source, "--catalog", lock, "--output", "json")
    assert result.exit_code == 2, result.output
    value = json.loads(result.output)
    assert value["errorCount"] == 1 and value["artifact"] is None and not value["ok"]
    assert value["diagnostics"][0]["code"].startswith("WV-CLI-")
    assert "do-not-echo-this" not in result.output


def test_warning_promotion(tmp_path):
    source = tmp_path / "warning.json"
    source.write_text(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "warning", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {"type": "string"},
                    "outputSchema": {"type": "string", "pattern": "^a"},
                    "steps": [],
                    "output": {"ref": "/input"},
                },
            }
        )
    )
    normal = invoke("workflow", "compile", source, "--catalog", LOCK, "--output", "json")
    strict = invoke("workflow", "compile", source, "--catalog", LOCK, "--output", "json", "--strict")
    assert normal.exit_code == 0 and strict.exit_code == 1
    assert any(d["severity"] == "warning" for d in json.loads(normal.output)["diagnostics"])
    assert all(d["severity"] == "error" for d in json.loads(strict.output)["diagnostics"])
    assert json.loads(strict.output)["artifact"] is None


def test_explain_contains_versions_flow_positions_and_guards():
    result = invoke("workflow", "explain", SOURCE, "--catalog", LOCK)
    assert result.exit_code == 0, result.output
    for expected in (
        "onboarding.check-customer@1.0.0",
        "@start -> check",
        "check -> approval",
        "/input/customerId",
        "/steps/check/output/eligible",
        "workflow_input",
        "schema=",
        ":21:7",
    ):
        assert expected in result.output


def test_parse_truncation_survives_json(tmp_path):
    source = tmp_path / "duplicates.yaml"
    source.write_text("kind: Workflow\n" * 120)
    result = invoke("workflow", "validate", source, "--output", "json")
    value = json.loads(result.output)
    assert result.exit_code == 1 and value["truncated"]
    assert value["omittedCount"] > 0 and len(value["diagnostics"]) == 100


def test_source_size_is_invalid_definition(tmp_path):
    source = tmp_path / "large.yaml"
    source.write_bytes(b" " * 1_048_577)
    result = invoke("workflow", "validate", source, "--output", "json")
    assert result.exit_code == 1
    assert json.loads(result.output)["artifact"] is None


@pytest.mark.parametrize("kind", ["schema", "compile"])
def test_export_requires_explicit_overwrite_and_preserves_unrelated_files(tmp_path, kind):
    directory = tmp_path / "export"
    args = ["schema", "export"] if kind == "schema" else ["workflow", "compile", SOURCE, "--catalog", LOCK]
    args += ["--directory", directory, "--output", "json"]
    first = invoke(*args)
    assert first.exit_code == 0, first.output
    json.loads(first.output)
    target = directory / ("workflow.schema.json" if kind == "schema" else "compiled-artifact.json")
    assert target.is_file()
    target.write_text("keep-me")
    unrelated = directory / "notes.txt"
    unrelated.write_text("untouched")
    blocked = invoke(*args)
    assert blocked.exit_code == 2, blocked.output
    assert target.read_text() == "keep-me"
    forced = invoke(*args, "--force")
    assert forced.exit_code == 0, forced.output
    assert target.read_text() != "keep-me" and unrelated.read_text() == "untouched"
    if kind == "compile":
        artifact = import_artifact(target.read_bytes())
        assert json.loads((directory / "executable.json").read_bytes()) == artifact.executable
        snapshot = CatalogSnapshot.from_lock(
            parse_source((directory / "catalog.lock.json").read_bytes(), format="json").value
        )
        assert snapshot.resolve("Action", "onboarding.check-customer@1.0.0")
    else:
        assert {"catalog-lock.schema.json", "executable.schema.json", "compiled-artifact.schema.json"} <= {
            p.name for p in directory.iterdir()
        }


def test_export_preflights_all_files_and_rejects_symlinks_even_with_force(tmp_path):
    directory = tmp_path / "export"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("untouched")
    (directory / "workflow.schema.json").symlink_to(target)
    result = invoke("schema", "export", "--directory", directory, "--force", "--output", "json")
    assert result.exit_code == 2
    assert target.read_text() == "untouched"
    assert list(directory.iterdir()) == [directory / "workflow.schema.json"]


def test_partial_compile_does_not_write_export(tmp_path):
    result = invoke("workflow", "compile", SOURCE, "--directory", tmp_path / "out", "--output", "json")
    assert result.exit_code == 1 and not (tmp_path / "out").exists()


def test_all_commands_offline_in_fresh_interpreter(tmp_path):
    script = r"""
import importlib.abc, json, socket, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = {'pyfly', 'asyncpg', 'sqlalchemy', 'httpx', 'keyring'}
        if fullname.split('.')[0] in forbidden or fullname.startswith('firefly_weave.app'):
            raise AssertionError('Forbidden startup import: ' + fullname)
sys.meta_path.insert(0, Deny())
def deny(*args, **kwargs):
    raise AssertionError('Network unavailable')
socket.socket = deny
socket.create_connection = deny
from click.testing import CliRunner
from firefly_weave.cli.main import cli
from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from pathlib import Path
source, lock, destination = sys.argv[1:]
compiled = compile_source(Path(source).read_bytes(), format="yaml",
    catalog=CatalogSnapshot.from_lock(json.loads(Path(lock).read_text())))
assert compiled.ok and import_artifact(compiled.artifact.to_bytes()).digest
for command in ('validate', 'compile', 'explain'):
    result = CliRunner().invoke(cli, ['workflow', command, source, '--catalog', lock, '--output', 'json'])
    assert result.exit_code == 0, (result.output, result.exception)
    assert import_artifact(json.loads(result.output)['artifact']).digest
for args in (['version'], ['schema', 'export', '--directory', destination, '--output', 'json']):
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, (result.output, result.exception)
assert not any(name.split('.')[0] in {'pyfly','asyncpg','sqlalchemy','httpx','keyring'} for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(SOURCE), str(LOCK), str(tmp_path / "schemas")],
        capture_output=True,
        text=True,
        env={**os.environ, "WEAVE_SECRET": "must-not-appear"},
    )
    assert result.returncode == 0, result.stderr
    assert "must-not-appear" not in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["workflow", "compile", "--output", "json"],
        ["workflow", "compile", str(SOURCE), "--unknown=do-not-echo", "--output=json"],
        ["schema", "export", "--output", "json"],
    ],
)
def test_invocation_errors_stay_machine_readable(arguments):
    result = invoke(*arguments)
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-USAGE"
    assert "do-not-echo" not in result.output


@pytest.mark.parametrize("obstacle", ["destination-file", "destination-link", "target-directory", "late-conflict"])
def test_export_refuses_unsafe_destination_without_partial_writes(tmp_path, obstacle):
    directory = tmp_path / "export"
    if obstacle == "destination-file":
        directory.write_text("untouched")
    elif obstacle == "destination-link":
        directory.symlink_to(tmp_path, target_is_directory=True)
    else:
        directory.mkdir()
        target = directory / "workflow.schema.json"
        if obstacle == "target-directory":
            target.mkdir()
        else:
            target.write_text("untouched")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    args = ["schema", "export", "--directory", directory, "--output", "json"]
    if obstacle != "late-conflict":
        args.append("--force")
    result = invoke(*args)
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-EXPORT"
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def test_explain_distinguishes_object_keys_from_expression_tags(tmp_path):
    source = tmp_path / "keys.json"
    source.write_text(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "keys", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {"type": "boolean"},
                    "outputSchema": {},
                    "steps": [],
                    "output": {
                        "object": {
                            "literal": {"ref": "/input"},
                            "ref": {"literal": {"ref": "/never-read-literal-data"}},
                        }
                    },
                },
            }
        )
    )
    result = invoke("workflow", "explain", source, "--catalog", LOCK)
    assert result.exit_code == 0, result.output
    assert "reads /input" in result.output
    assert "/never-read-literal-data" not in result.output


def test_admin_migrate_help_does_not_load_server():
    code = (
        "import sys; from click.testing import CliRunner; from firefly_weave.cli.main import cli; "
        "result=CliRunner().invoke(cli,['admin','migrate','--help']); "
        "assert result.exit_code == 0, result.output; "
        "assert not any(x in sys.modules for x in ['sqlalchemy','pyfly','firefly_weave.settings'])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
