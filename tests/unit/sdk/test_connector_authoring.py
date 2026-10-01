# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Offline authoring must validate data without importing trusted package code."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_scaffold_does_not_overwrite_author_work(tmp_path):
    (tmp_path / "README.md").write_text("keep")
    result = CliRunner().invoke(cli, ["connector", "init", str(tmp_path), "--name", "acme-echo"])
    assert result.exit_code != 0
    assert (tmp_path / "README.md").read_text() == "keep"


def test_scaffold_is_installable_and_validation_does_not_import(tmp_path):
    result = CliRunner().invoke(cli, ["connector", "init", str(tmp_path / "sample"), "--name", "acme-echo"])
    assert result.exit_code == 0, result.output
    target = tmp_path / "sample"
    module = target / "src/acme_echo/__init__.py"
    module.write_text('raise RuntimeError("must not import")')
    result = CliRunner().invoke(cli, ["connector", "validate", str(target / "connector.json"), "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == "offline-data"
    assert (target / "tests/test_conformance.py").exists()
    assert (target / "examples/action.json").exists()
    assert (target / "examples/provider_verifier.py").exists()


@pytest.mark.parametrize("name", ['a";import os', "../escape", "weave-http", "class", "a.b", "a\nname"])
def test_scaffold_rejects_unsafe_or_reserved_names(tmp_path, name):
    result = CliRunner().invoke(cli, ["connector", "init", str(tmp_path / "sample"), "--name", name])
    assert result.exit_code != 0
    assert not (tmp_path / "sample").exists()


def test_metadata_and_canonical_schema_validation(tmp_path):
    from firefly_weave.sdk.connectors import scaffold, validate

    target = tmp_path / "sample"
    scaffold(target, "acme-echo")
    path = target / "connector.json"
    valid = validate(path)
    assert valid.descriptor.manifest.digest == validate(path).descriptor.manifest.digest
    copy = valid.document
    copy["family"] = "changed"
    assert valid.document["family"] == "custom"
    document = json.loads(path.read_text())
    document["event_schemas"] = {"message": {"$ref": "https://invalid.example/schema"}}
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="schema"):
        validate(path)
    document["event_schemas"] = {}
    document["capabilities"][0]["sideEffect"] = "non_idempotent"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="capability"):
        validate(path)


def test_authoring_modules_have_no_infrastructure_imports():
    code = """
import sys
from firefly_weave.sdk.connectors import scaffold, validate
assert not any(n.split('.')[0] in {'pyfly', 'sqlalchemy', 'asyncpg', 'httpx', 'aiokafka'} for n in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], cwd=Path(sys.executable).parent, check=True)


def test_generated_action_compiles(tmp_path):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.sdk.connectors import scaffold, validate

    scaffold(tmp_path, "acme-echo")
    metadata = validate(tmp_path / "connector.json")
    result = compile_source(
        (tmp_path / "examples/action.json").read_bytes(),
        format="json",
        catalog=CatalogSnapshot.from_definitions(
            [metadata.model.manifest], tasks=metadata.model.capabilities, adapters=["acme-echo"]
        ),
    )
    assert result.ok, result.diagnostics


@pytest.mark.parametrize("change", ["manifest", "binding", "duplicate", "reserved", "unknown", "schema", "remote"])
def test_rejects_declaration_drift_and_malformed_schema(tmp_path, change):
    from firefly_weave.sdk.connectors import scaffold, validate

    scaffold(tmp_path, "acme-echo")
    path = tmp_path / "connector.json"
    data = json.loads(path.read_text())
    if change == "manifest":
        data["manifest"]["spec"]["actions"]["echo"]["sideEffect"] = "non_idempotent"
    elif change == "binding":
        data["bindings"][0]["connector_digest"] = "f" * 64
    elif change == "duplicate":
        data["capabilities"].append(data["capabilities"][0])
    elif change == "reserved":
        data["manifest"]["spec"]["adapter"] = "weave-http"
    elif change == "unknown":
        data["surprise"] = True
    else:
        data["verifier_service"] = "acme_echo:Verifier"
        data["event_schemas"] = {"message": {"type": "bad"} if change == "schema" else {"$ref": "https://bad/schema"}}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        validate(path)


def test_scaffold_refuses_symlink(tmp_path):
    from firefly_weave.sdk.connectors import scaffold

    target = tmp_path / "link"
    target.symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError):
        scaffold(target, "acme-echo")
    assert not (tmp_path / "real").exists()


def test_package_rejects_resource_drift_before_executing_build(tmp_path):
    from firefly_weave.sdk.connectors import package, scaffold

    scaffold(tmp_path / "sample", "acme-echo")
    resource = tmp_path / "sample/src/acme_echo/connector.json"
    data = json.loads(resource.read_text())
    data["family"] = "changed"
    resource.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="resource"):
        package(tmp_path / "sample", tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


def test_direct_metadata_has_same_source_budget(tmp_path):
    from firefly_weave.compiler.catalog import FrozenDocument
    from firefly_weave.connectors.packages import PackageMetadata
    from firefly_weave.sdk.connectors import scaffold, validate

    scaffold(tmp_path, "acme-echo")
    document = validate(tmp_path / "connector.json").document
    document["evidence"] = ["x" * 1048577]
    with pytest.raises(ValueError, match="budget|RESOURCE_LIMIT"):
        PackageMetadata(FrozenDocument.from_value(document))


def test_package_emits_one_machine_json_result(tmp_path):
    from firefly_weave.sdk.connectors import scaffold

    scaffold(tmp_path / "sample", "acme-echo")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "firefly_weave.cli.main",
            "connector",
            "package",
            str(tmp_path / "sample"),
            "--directory",
            str(tmp_path / "dist"),
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "local-build"
    assert len(list((tmp_path / "dist").glob("*.whl"))) == 1


def test_package_uses_exact_distribution_version_independent_of_adapter(tmp_path):
    from firefly_weave.sdk.connectors import package, scaffold

    root = tmp_path / "alpha"
    scaffold(root, "alpha-adapter")
    for path in (root / "connector.json", root / "src/alpha_adapter/connector.json"):
        data = json.loads(path.read_text())
        data["distribution_version"] = "0.1.0a1"
        path.write_text(json.dumps(data))
    project = root / "pyproject.toml"
    project.write_text(project.read_text().replace('version = "1.0.0"', 'version = "0.1.0a1"'))
    package(root, tmp_path / "dist")
    assert (tmp_path / "dist/alpha_adapter-0.1.0a1-py3-none-any.whl").is_file()
