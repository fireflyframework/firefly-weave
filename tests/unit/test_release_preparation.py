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

"""Release context inputs stay independent and source-free."""

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_context_preparation_uses_one_exact_wheel(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("prepare_release", ROOT / "scripts/prepare_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if "build" in argv:
            artifacts = Path(argv[argv.index("--out-dir") + 1])
            artifacts.mkdir()
            (artifacts / ".gitignore").write_text("*\n")
            (artifacts / "firefly_weave-0.1.0a4-py3-none-any.whl").write_bytes(b"exact wheel bytes")
            with tarfile.open(artifacts / "firefly_weave-0.1.0a4.tar.gz", "w:gz"):
                pass
            return b"built"
        return b"click==8.5.0 --hash=sha256:" + b"1" * 64 + b"\n"

    monkeypatch.setattr(module, "run_command", command)
    destination = tmp_path / "release"
    value = module.prepare(ROOT, destination)
    assert value["complete"] is True
    context = destination / "images"
    assert not (context / "src").exists()
    assert not (context / "tests").exists()
    assert (context / "firefly_weave-0.1.0a4-py3-none-any.whl").read_bytes() == b"exact wheel bytes"
    assert {p.name for p in context.glob("*-requirements.txt")} == {
        "base-requirements.txt",
        "worker-requirements.txt",
        "server-requirements.txt",
        "teams-requirements.txt",
        "kafka-requirements.txt",
    }
    exports = [args for args in calls if "export" in args]
    assert all("--quiet" in args for args in exports)
    assert len(exports) == 6 and all("--locked" in args and "--no-emit-project" in args for args in exports)
    assert not any("docker" in args for args in calls)
    cli_export = exports[-1]
    assert cli_export[-4:] == ["--extra", "client", "--extra", "openapi"]
    assets = destination / "artifacts"
    installer = json.loads((assets / "cli-install.json").read_text())
    assert installer == {
        "schema_version": 1,
        "version": "0.1.0a4",
        "wheel": value["wheel"],
        "wheel_sha256": value["wheel_sha256"],
        "requirements": "cli-requirements.txt",
        "requirements_sha256": hashlib.sha256((assets / "cli-requirements.txt").read_bytes()).hexdigest(),
    }
    checksums = dict(line.split("  ")[::-1] for line in (assets / "SHA256SUMS").read_text().splitlines())
    assert set(checksums) == {value["wheel"], value["sdist"], "cli-install.json", "cli-requirements.txt", "install.sh"}
    for name, digest in checksums.items():
        assert hashlib.sha256((assets / name).read_bytes()).hexdigest() == digest
    assert (assets / "install.sh").read_bytes() == (ROOT / "install.sh").read_bytes()
    assert json.loads((destination / "release.json").read_text())["wheel_sha256"] == value["wheel_sha256"]


def test_production_docker_targets_do_not_inherit_server_for_worker():
    main = (ROOT / "Dockerfile").read_text()
    worker = (ROOT / "docker/worker.Dockerfile").read_text()
    assert "FROM runtime AS worker" not in main
    assert "COPY src" not in main + worker
    assert "uv sync" not in main + worker
    assert "--require-hashes" in main and "--require-hashes" in worker
    assert "worker-requirements.txt" in worker
    assert "server-requirements.txt" not in worker
    assert "@sha256:" in main and "@sha256:" in worker
