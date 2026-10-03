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

"""Independent workers are immutable, separately installed and source-safe."""

import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def tooling():
    spec = importlib.util.spec_from_file_location("release_workers", ROOT / "scripts/release_workers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wheel(name, *, license=True, extra=None):
    buffer = io.BytesIO()
    prefix = f"weave_{name}_worker-0.1.0.dist-info"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"weave_{name}_worker/__init__.py", "")
        archive.writestr(prefix + "/METADATA", f"Name: weave-{name}-worker\nVersion: 0.1.0\n")
        if license:
            for item in ("LICENSE", "NOTICE"):
                archive.writestr(prefix + "/licenses/" + item, item)
        if extra:
            archive.writestr(extra, "private")
    return buffer.getvalue()


def release(tmp_path):
    root = tmp_path / "release"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    core = "firefly_weave-0.1.0a9-py3-none-any.whl"
    records = {}
    for name, python in tooling().WORKERS.items():
        record = {
            "python": python,
            "wheel": f"weave_{name}_worker-0.1.0-py3-none-any.whl",
            "sdist": f"weave_{name}_worker-0.1.0.tar.gz",
            "requirements": f"{name}-worker-requirements.txt",
        }
        data = {
            "wheel": wheel(name),
            "sdist": b"not extracted before validation",
            "requirements": b"click==8.5.0 --hash=sha256:" + b"1" * 64 + b"\n",
        }
        for key, value in data.items():
            (artifacts / record[key]).write_bytes(value)
            record[key + "_sha256"] = hashlib.sha256(value).hexdigest()
        context = root / "worker-images" / name
        context.mkdir(parents=True)
        inputs = {
            core: b"core",
            record["wheel"]: data["wheel"],
            record["requirements"]: data["requirements"],
            **{item: b"input" for item in ("Dockerfile", "image_identity.py", "LICENSE", "NOTICE")},
        }
        for filename, value in inputs.items():
            (context / filename).write_bytes(value)
        manifest = json.dumps(
            {
                "wheel_sha256": hashlib.sha256(b"core").hexdigest(),
                "inputs": {key: hashlib.sha256(value).hexdigest() for key, value in inputs.items()},
            }
        ).encode()
        (context / "release.json").write_bytes(manifest)
        (context / ".dockerignore").write_text("**\n")
        record["context_sha256"] = hashlib.sha256(manifest).hexdigest()
        records[name] = record
    metadata = {
        "complete": True,
        "wheel": core,
        "wheel_sha256": hashlib.sha256(b"core").hexdigest(),
        "workers": records,
    }
    (root / "release.json").write_text(json.dumps(metadata))
    return root, metadata


@pytest.mark.parametrize("name", ["agentic", "files"])
@pytest.mark.parametrize("member", ["wheel", "sdist", "requirements", "Dockerfile"])
def test_changed_worker_input_fails_before_any_install(tmp_path, monkeypatch, name, member):
    module = tooling()
    root, metadata = release(tmp_path)
    target = (
        root / "worker-images" / name / "Dockerfile"
        if member == "Dockerfile"
        else root / "artifacts" / metadata["workers"][name][member]
    )
    target.write_bytes(b"changed")
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="identity|input"):
        module.verify(root, tmp_path / "verified")
    assert calls == []
    assert not (tmp_path / "verified").exists()


def test_worker_closures_are_both_required_and_python_is_pinned(tmp_path):
    root, metadata = release(tmp_path)
    module = tooling()
    assert set(module.validate(root, metadata)) == {"agentic", "files"}
    metadata["workers"]["agentic"]["python"] = "3.12"
    with pytest.raises(ValueError, match="Python boundary"):
        module.validate(root, metadata)
    metadata["workers"].pop("agentic")
    with pytest.raises(ValueError, match="inventory"):
        module.validate(root, metadata)


@pytest.mark.parametrize("extra", ["../outside", "weave_agentic_worker/.env", "weave_agentic_worker/AGENTS.md"])
def test_worker_wheel_rejects_unsafe_members(extra):
    with pytest.raises(ValueError, match="Unsafe"):
        tooling().worker_wheel(wheel("agentic", extra=extra), "weave_agentic_worker-0.1.0-py3-none-any.whl", "agentic")


def test_worker_wheel_requires_shipped_license_and_notice():
    with pytest.raises(KeyError):
        tooling().worker_wheel(wheel("files", license=False), "weave_files_worker-0.1.0-py3-none-any.whl", "files")


@pytest.mark.parametrize("member", ["../../outside", "src/.env", "src/AGENTS.md", "private-notes.md"])
def test_worker_sdist_rejects_private_or_escaping_content_before_extract(tmp_path, member):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        item = tarfile.TarInfo("worker-0.1.0/" + member)
        item.size = 1
        archive.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        tooling().extract_sdist(data.getvalue(), tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize("inherited_python", [None, "3.12", "3.13"])
def test_worker_preparation_selects_its_python_despite_parent_environment(tmp_path, monkeypatch, inherited_python):
    module = tooling()
    if inherited_python is None:
        monkeypatch.delenv("UV_PYTHON", raising=False)
    else:
        monkeypatch.setenv("UV_PYTHON", inherited_python)
    destination = tmp_path / "release"
    artifacts = destination / "artifacts"
    artifacts.mkdir(parents=True)
    core = "firefly_weave-0.1.0a9-py3-none-any.whl"
    (artifacts / core).write_bytes(b"core")
    selected = []

    def command(arguments, **kwargs):
        name = Path(arguments[arguments.index("--project") + 1]).name
        python = arguments[arguments.index("--python") + 1] if "--python" in arguments else inherited_python
        assert python == module.WORKERS[name], "Worker preparation inherited the parent's Python selection"
        selected.append((name, arguments[1], python))
        if arguments[1] == "build":
            output = Path(arguments[arguments.index("--out-dir") + 1])
            output.mkdir()
            (output / f"weave_{name}_worker-0.1.0-py3-none-any.whl").write_bytes(wheel(name))
            (output / f"weave_{name}_worker-0.1.0.tar.gz").write_bytes(b"sdist")
            return b"built"
        return b"click==8.5.0 --hash=sha256:" + b"1" * 64 + b"\n"

    monkeypatch.setattr(module, "run_command", command)
    records = module.prepare(ROOT, destination, core)
    assert selected == [
        ("agentic", "build", "3.13"),
        ("agentic", "export", "3.13"),
        ("files", "build", "3.12"),
        ("files", "export", "3.12"),
    ]
    assert {name: record["python"] for name, record in records.items()} == module.WORKERS
