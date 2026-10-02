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
"""Exercise source coverage as a command against isolated, intentionally broken trees."""

import errno
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "source_coverage.py"
HEADER = (
    "\n".join(
        [
            "# Copyright 2026 Firefly Software Foundation.",
            '# Licensed under the Apache License, Version 2.0 (the "License");',
            "# you may not use this file except in compliance with the License.",
            "# You may obtain a copy of the License at",
            "# http://www.apache.org/licenses/LICENSE-2.0",
            "# Unless required by applicable law or agreed to in writing, software",
            '# distributed under the License is distributed on an "AS IS" BASIS,',
            "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
            "# See the License for the specific language governing permissions and",
            "# limitations under the License.",
            "# Author: Firefly Software Foundation",
            "# SPDX-License-Identifier: Apache-2.0",
        ]
    )
    + "\n"
)


def run(root, *flags):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--format", "json", *flags],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def config(root, content):
    path = root / "docs/contributing/source-inventory.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "version = 1\n" + content)


def test_reports_missing_headers_and_docstrings_with_strict_exit(tmp_path):
    (tmp_path / "missing.py").write_text("value = 1\n")
    code, report = run(tmp_path)
    assert code == 0
    assert {issue["code"] for issue in report["issues"]} >= {"missing-header", "missing-docstring"}
    assert run(tmp_path, "--strict")[0] == 1
    (tmp_path / "missing.py").write_text(HEADER + '"""Load the sample configuration."""\nvalue = 1\n')
    assert run(tmp_path, "--strict")[0] == 0


def test_header_strings_in_code_and_partial_boilerplate_do_not_count(tmp_path):
    (tmp_path / "fake.py").write_text('"""A module storing strings for a protocol fixture."""\nvalue = ' + repr(HEADER))
    assert run(tmp_path, "--strict")[0] == 1
    (tmp_path / "fake.py").write_text(
        HEADER.replace("limitations under the License.", "incomplete") + '"""Load configuration."""'
    )
    assert "missing-header" in {x["code"] for x in run(tmp_path)[1]["issues"]}


def test_shebang_encoding_and_syntax_are_checked(tmp_path):
    path = tmp_path / "tool.py"
    path.write_text("#!/usr/bin/env python3\n# coding: utf-8\n" + HEADER + '"""Read a local manifest."""\n')
    assert run(tmp_path, "--strict")[0] == 0
    path.write_text(HEADER + '"""Read a local manifest."""\ndef broken(:')
    assert "python-syntax" in {x["code"] for x in run(tmp_path)[1]["issues"]}


def test_exact_commentless_exception_preserves_bytes_and_new_json_fails(tmp_path):
    (tmp_path / "sample.json").write_bytes(b'{"fixed":true}')
    config(
        tmp_path,
        """
[[exceptions]]
path = "sample.json"
kind = "commentless"
reason = "Strict parser fixture; comments would change the contract."
copyright = "Copyright 2026 Firefly Software Foundation."
author = "Firefly Software Foundation"
license = "Apache-2.0"
""",
    )
    assert run(tmp_path, "--strict")[0] == 0
    assert (tmp_path / "sample.json").read_bytes() == b'{"fixed":true}'
    (tmp_path / "new.json").write_text("{}")
    report = run(tmp_path, "--strict")
    assert report[0] == 1
    assert any(x["path"] == "new.json" and x["code"] == "uncovered-format" for x in report[1]["issues"])


def test_exact_hashed_source_map_fixture_rejects_byte_drift(tmp_path):
    fixture = tmp_path / "tests/fixtures/definitions/duplicate-key.yaml"
    fixture.parent.mkdir(parents=True)
    source = b"name: one\nname: two\n"
    fixture.write_bytes(source)
    config(
        tmp_path,
        f'''
[[exceptions]]
path = "tests/fixtures/definitions/duplicate-key.yaml"
kind = "immutable-fixture"
reason = "Parser assertion fixes the duplicate key on line 2."
copyright = "Copyright 2026 Firefly Software Foundation."
author = "Firefly Software Foundation"
license = "Apache-2.0"
sha256 = "{hashlib.sha256(source).hexdigest()}"
''',
    )
    assert run(tmp_path, "--strict")[0] == 0
    fixture.write_bytes(b"# annotation\n" + source)
    code, report = run(tmp_path, "--strict")
    assert code == 1
    assert any(
        x["path"] == "tests/fixtures/definitions/duplicate-key.yaml" and x["code"] == "fixture-drift"
        for x in report["issues"]
    )


def test_rendered_source_templates_need_native_headers_and_python_docstrings(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    markup = "<!--\n" + "\n".join(line.removeprefix("# ") for line in HEADER.splitlines()) + "\n-->\n"
    (templates / "README.md.tmpl").write_text(markup + "# Example\n")
    (templates / "pyproject.toml.tmpl").write_text(HEADER + '[project]\nname = "sample"\n')
    module = templates / "adapter.py.tmpl"
    module.write_text(HEADER + '"""Own the generated adapter boundary."""\n')
    assert run(tmp_path, "--strict")[0] == 0
    module.write_text(HEADER + "value = 1\n")
    assert any(
        x["path"] == "templates/adapter.py.tmpl" and x["code"] == "missing-docstring"
        for x in run(tmp_path, "--strict")[1]["issues"]
    )


def test_stale_duplicate_and_broad_exceptions_are_rejected(tmp_path):
    config(tmp_path, '[[exceptions]]\npath = "missing.json"\nkind = "commentless"\nreason = "Fixture"\n')
    assert run(tmp_path, "--strict")[0] == 1
    config(tmp_path, '[[exceptions]]\npath = "*.json"\nkind = "commentless"\nreason = "Fixture"\n')
    assert run(tmp_path)[0] == 2
    config(tmp_path, '[[exceptions]]\npath = "a"\n[[exceptions]]\npath = "a"\n')
    assert run(tmp_path)[0] == 2


def test_generated_and_third_party_require_provenance(tmp_path):
    (tmp_path / "generated.json").write_text("{}")
    (tmp_path / "upstream.txt").write_text("External work\n")
    config(
        tmp_path,
        """
[[exceptions]]
path = "generated.json"
kind = "generated"
reason = "Generated schema, preserve deterministic bytes."
copyright = "Copyright 2026 Firefly Software Foundation."
author = "Firefly Software Foundation"
license = "Apache-2.0"
source = "generator.py"
[[exceptions]]
path = "upstream.txt"
kind = "third-party"
reason = "Unmodified upstream data."
copyright = "Copyright Example Contributors"
license = "MIT"
source = "https://example.org/upstream"
""",
    )
    assert run(tmp_path, "--strict")[0] == 1
    (tmp_path / "generator.py").write_text(HEADER + '"""Export example schema data."""')
    assert run(tmp_path, "--strict")[0] == 0


def test_private_trees_are_pruned_and_external_symlinks_not_followed(tmp_path):
    for name in (".superpowers", ".venv", "dist", "node_modules", ".hypothesis"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "private.py").write_text("secret")
    (tmp_path / ".env.local").write_text("private")
    (tmp_path / "outside.py").symlink_to("/unavailable/source.py")
    (tmp_path / "unknown.blob").write_bytes(b"\x00")
    _, report = run(tmp_path)
    assert {x["path"] for x in report["issues"]} == {"outside.py", "unknown.blob"}
    assert run(tmp_path)[1] == report


def test_local_tutorial_outputs_are_excluded_while_source_stays_checked(tmp_path):
    tutorial = tmp_path / ".local/tutorial/compiled"
    tutorial.mkdir(parents=True)
    (tutorial / "compiled-artifact.json").write_text("{}")
    (tutorial.parent / "echo.workflow.yaml").write_text("kind: Workflow\n")
    (tmp_path / ".local/runtime.env").write_text("WEAVE_DATABASE_URL=private\n")
    source = tmp_path / "src/module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")

    code, report = run(tmp_path, "--strict")
    assert code == 1
    assert {item["path"] for item in report["files"]} == {"src/module.py"}
    assert {item["path"] for item in report["excluded"]} == {".local"}
    assert {(item["path"], item["code"]) for item in report["issues"]} == {
        ("src/module.py", "missing-header"),
        ("src/module.py", "missing-docstring"),
    }
    source.write_text(HEADER + '"""Own the example configuration."""\nvalue = 1\n')
    assert run(tmp_path, "--strict")[0] == 0


def test_xml_and_sql_headers_and_blank_docstrings(tmp_path):
    plain = "\n".join("    " + line.removeprefix("# ") for line in HEADER.splitlines())
    (tmp_path / "diagram.svg").write_text('<?xml version="1.0"?>\n<!--\n' + plain + "\n-->\n<svg/>")
    (tmp_path / "query.sql").write_text("\n".join("-- " + line for line in plain.splitlines()) + "\nSELECT 1;")
    (tmp_path / "empty.py").write_text(HEADER + '"""   """')
    _, report = run(tmp_path)
    assert [(x["path"], x["code"]) for x in report["issues"]] == [("empty.py", "missing-docstring")]


def test_css_requires_attribution_in_a_leading_block_comment(tmp_path):
    plain = "\n".join(line.removeprefix("# ") for line in HEADER.splitlines())
    source = tmp_path / "theme.css"
    source.write_text("/*\n" + plain + "\n*/\nbody { color: green; }\n")
    assert run(tmp_path, "--strict")[0] == 0
    source.write_text("body {}\n/*\n" + plain + "\n*/\n")
    code, report = run(tmp_path, "--strict")
    assert code == 1
    assert report["issues"][0]["code"] == "missing-header"


@pytest.mark.parametrize("blocked_child", [False, True])
def test_traversal_permission_errors_cannot_become_strict_success(tmp_path, monkeypatch, capsys, blocked_child):
    module = runpy.run_path(str(SCRIPT))
    child = tmp_path / "source"
    child.mkdir()
    (child / "module.py").write_text(HEADER + '"""Own the example configuration."""')
    blocked = child if blocked_child else tmp_path
    scandir = os.scandir

    def deny_selected_directory(path):
        if Path(path) == blocked:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", deny_selected_directory)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--root", str(tmp_path), "--strict", "--format", "json"])
    assert module["main"]() == 2
    assert json.loads(capsys.readouterr().out)["error"] == "PermissionError"


@pytest.mark.parametrize("explicit", [False, True])
def test_inventory_symlink_ancestors_are_rejected_before_reading(tmp_path, monkeypatch, explicit):
    module = runpy.run_path(str(SCRIPT))
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "external"
    (outside / "contributing").mkdir(parents=True)
    sentinel = outside / "contributing/source-inventory.toml"
    sentinel.write_text("version = 1\n")
    (root / "docs").symlink_to(outside, target_is_directory=True)
    selected = root / "docs/contributing/source-inventory.toml"
    flags = ("--inventory", str(selected)) if explicit else ()
    assert run(root, "--strict", *flags)[0] == 2
    read_text = Path.read_text

    def forbid_external_read(path, *args, **kwargs):
        if path.resolve() == sentinel.resolve():
            pytest.fail("The configuration symlink target was read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_external_read)
    with pytest.raises(ValueError, match="symlink"):
        module["load_exceptions"](selected)


def test_explicit_missing_inventory_is_configuration_failure(tmp_path):
    (tmp_path / "module.py").write_text(HEADER + '"""Own the example configuration."""')
    assert run(tmp_path, "--strict")[0] == 0
    code, report = run(tmp_path, "--strict", "--inventory", str(tmp_path / "missing.toml"))
    assert code == 2
    assert report["error"] == "FileNotFoundError"


@pytest.mark.parametrize("suffix", [".ts", ".js", ".mjs", ".scss"])
def test_studio_source_requires_full_block_header(tmp_path, suffix):
    body = "/*\n" + "\n".join(line.removeprefix("# ").removeprefix("#") for line in HEADER.splitlines()) + "\n*/\n"
    path = tmp_path / ("component" + suffix)
    path.write_text(body + "const example = 1;\n")
    assert run(tmp_path, "--strict")[0] == 0
    path.write_text("/* SPDX-License-Identifier: Apache-2.0 */\nconst example = 1;\n")
    assert "missing-header" in {item["code"] for item in run(tmp_path)[1]["issues"]}


def test_rust_block_header_is_recognized(tmp_path):
    header = "/*\n" + "\n".join(line.removeprefix("# ") for line in HEADER.splitlines()) + "\n*/\n"
    (tmp_path / "main.rs").write_text(header + "fn main() {}\n")
    assert run(tmp_path, "--strict")[0] == 0
