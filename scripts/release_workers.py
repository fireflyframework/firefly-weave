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

"""Prepare and verify independent workers against the same immutable core release."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from firefly_weave.sdk.deployment import read_file, real_path, requirements_bytes, run_command

WORKERS = {"agentic": "3.13", "files": "3.12"}
LIMIT = 64 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)


def worker_wheel(data: bytes, name: str, worker: str) -> dict[str, bytes]:
    package = f"weave_{worker}_worker"
    match = re.fullmatch(re.escape(package) + r"-([A-Za-z0-9_.+!]+)-py3-none-any\.whl", name)
    if match is None:
        raise ValueError("Unexpected worker wheel identity")
    info = f"{package}-{match[1]}.dist-info"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set()
        total = 0
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            total += member.file_size
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "%" in member.filename
                or "\\" in member.filename
                or path.as_posix() != member.filename.rstrip("/")
                or path.parts[0] not in {package, info}
                or any(part.startswith(".") or part in {"AGENTS.md", "CLAUDE.md", "__pycache__"} for part in path.parts)
                or member.filename in names
                or member.orig_filename != member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
                or member.flag_bits & 1
                or member.file_size > 16 * 1024 * 1024
                or total > LIMIT
            ):
                raise ValueError("Unsafe worker wheel member")
            names.add(member.filename)
        metadata = BytesParser().parsebytes(archive.read(info + "/METADATA"))
        if metadata.get_all("Name") != [package.replace("_", "-")] or metadata.get_all("Version") != [match[1]]:
            raise ValueError("Worker metadata identity mismatch")
        for legal in ("LICENSE", "NOTICE"):
            if not archive.read(info + "/licenses/" + legal):
                raise ValueError("Worker license or notice missing")
        return {name: archive.read(name) for name in names if name.startswith(package + "/")}


def prepare(root: Path, release: Path, core_wheel: str) -> dict:
    artifacts = release / "artifacts"
    core = read_file(artifacts / core_wheel, LIMIT)
    results = {}
    for name, python in WORKERS.items():
        source = root / "workers" / name
        built = release / (name + "-build")
        run_command(
            ["uv", "build", "--project", str(source), "--out-dir", str(built)],
            timeout=180,
            log_path=release / (name + "-build.log"),
        )
        wheels, sdists = list(built.glob("*.whl")), list(built.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Expected exactly one independent worker wheel and sdist")
        wheel, sdist = wheels[0], sdists[0]
        wheel_data, sdist_data = read_file(wheel, LIMIT), read_file(sdist, LIMIT)
        worker_wheel(wheel_data, wheel.name, name)
        requirements = requirements_bytes(
            run_command(
                [
                    "uv",
                    "export",
                    "--quiet",
                    "--project",
                    str(source),
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-emit-package",
                    "firefly-weave",
                    "--no-header",
                    "--no-annotate",
                ],
                timeout=60,
                log_path=release / (name + "-export.log"),
            )
        )
        requirements_name = name + "-worker-requirements.txt"
        for filename, content in (
            (wheel.name, wheel_data),
            (sdist.name, sdist_data),
            (requirements_name, requirements),
        ):
            write(artifacts / filename, content)
        context = release / "worker-images" / name
        context.mkdir(parents=True, mode=0o700)
        inputs = {
            core_wheel: core,
            wheel.name: wheel_data,
            requirements_name: requirements,
            "Dockerfile": read_file(root / "docker" / (name + "-worker.Dockerfile"), 1024 * 1024),
            "image_identity.py": read_file(root / "scripts/image_identity.py", 1024 * 1024),
            "LICENSE": read_file(root / "LICENSE", 1024 * 1024),
            "NOTICE": read_file(root / "NOTICE", 1024 * 1024),
        }
        for filename, content in inputs.items():
            write(context / filename, content)
        manifest = {"wheel_sha256": digest(core), "inputs": {key: digest(value) for key, value in inputs.items()}}
        manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        write(context / "release.json", manifest_data)
        write(context / ".dockerignore", read_file(root / ".dockerignore", 1024 * 1024) + f"\n!{wheel.name}\n".encode())
        results[name] = {
            "python": python,
            "wheel": wheel.name,
            "wheel_sha256": digest(wheel_data),
            "sdist": sdist.name,
            "sdist_sha256": digest(sdist_data),
            "requirements": requirements_name,
            "requirements_sha256": digest(requirements),
            "context_sha256": digest(manifest_data),
        }
    write(release / "workers.json", (json.dumps(results, sort_keys=True, indent=2) + "\n").encode())
    return results


def validate(release: Path, metadata: dict) -> dict[str, dict[str, bytes]]:
    if metadata.get("complete") is not True or not re.fullmatch(
        r"firefly_weave-[A-Za-z0-9_.+!]+-py3-none-any\.whl", metadata.get("wheel", "")
    ):
        raise ValueError("A complete core release with a safe wheel identity is required")
    records = metadata.get("workers", {})
    if set(records) != set(WORKERS):
        raise ValueError("Exact independent worker inventory required")
    snapshots = {}
    for name, python in WORKERS.items():
        record = records[name]
        if record.get("python") != python:
            raise ValueError("Independent worker Python boundary changed")
        files = {}
        for key in ("wheel", "sdist", "requirements"):
            filename = record[key]
            if Path(filename).name != filename:
                raise ValueError("Invalid worker artifact path")
            data = read_file(release / "artifacts" / filename, LIMIT)
            if digest(data) != record[key + "_sha256"]:
                raise ValueError("Worker artifact identity mismatch")
            files[filename] = data
        worker_wheel(files[record["wheel"]], record["wheel"], name)
        requirements_bytes(files[record["requirements"]])
        context = release / "worker-images" / name
        raw = read_file(context / "release.json", 1024 * 1024)
        if digest(raw) != record["context_sha256"]:
            raise ValueError("Worker context manifest changed")
        manifest = json.loads(raw)
        expected = {
            metadata["wheel"],
            record["wheel"],
            record["requirements"],
            "Dockerfile",
            "image_identity.py",
            "LICENSE",
            "NOTICE",
        }
        if set(manifest["inputs"]) != expected or {p.name for p in context.iterdir()} != expected | {
            "release.json",
            ".dockerignore",
        }:
            raise ValueError("Worker image context inventory changed")
        if manifest["wheel_sha256"] != metadata["wheel_sha256"]:
            raise ValueError("Worker core wheel identity changed")
        for filename, expected_hash in manifest["inputs"].items():
            data = read_file(context / filename, LIMIT)
            if digest(data) != expected_hash or (filename in files and data != files[filename]):
                raise ValueError("Worker image input changed")
            if filename == metadata["wheel"] and digest(data) != metadata["wheel_sha256"]:
                raise ValueError("Worker core wheel changed")
        snapshots[name] = files
    return snapshots


def extract_sdist(data: bytes, target: Path) -> Path:
    roots = set()
    names = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            total += member.size
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "%" in member.name
                or "\\" in member.name
                or path.as_posix() != member.name.rstrip("/")
                or member.name in names
                or not (member.isfile() or member.isdir())
                or total > LIMIT
                or any(
                    part.startswith(".") and not (index == 1 and len(path.parts) == 2 and part == ".gitignore")
                    for index, part in enumerate(path.parts)
                )
                or set(path.parts) & {"AGENTS.md", "CLAUDE.md", "__pycache__"}
                or (
                    len(path.parts) > 1
                    and path.parts[1]
                    not in {
                        "src",
                        "tests",
                        "pyproject.toml",
                        "uv.lock",
                        "LICENSE",
                        "NOTICE",
                        "Dockerfile",
                        "Dockerfile.dockerignore",
                        "PKG-INFO",
                        ".gitignore",
                    }
                )
            ):
                raise ValueError("Unsafe worker sdist member")
            roots.add(path.parts[0])
            names.add(member.name)
        if len(roots) != 1:
            raise ValueError("Expected one worker sdist root")
        target.mkdir(mode=0o700)
        archive.extractall(target, filter="data")
    return target / roots.pop()


PROBE = r"""
import importlib, importlib.metadata, json, pathlib, sys
worker = sys.argv[1]
name = 'weave_' + worker + '_worker'
package = importlib.import_module(name)
assert pathlib.Path(package.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
from firefly_weave.sdk.transport import WorkerTransport
assert hasattr(WorkerTransport, 'context') and hasattr(WorkerTransport, 'read_file')
core_version = importlib.metadata.version('firefly-weave')
assert core_version == sys.argv[2]
assert any(item in ('firefly-weave==' + core_version, 'firefly-weave[worker]==' + core_version)
           for item in importlib.metadata.requires(name))
assert any(item.group == 'console_scripts' and item.name == 'weave-' + worker + '-worker'
           and item.value == name + '.main:run' for item in importlib.metadata.distribution(name).entry_points)
if worker == 'agentic':
    from fireflyframework_agentic.agents.base import FireflyAgent
    from weave_agentic_worker.gateway import create_app
    assert sys.version_info[:2] == (3, 13)
print(json.dumps({'worker': worker, 'python': sys.version.split()[0], 'version': importlib.metadata.version(name),
                  'core_version': core_version}))
"""


def verify(release: Path, output: Path) -> dict:
    metadata = json.loads(read_file(release / "release.json", 1024 * 1024))
    snapshots = validate(release, metadata)
    output.mkdir(mode=0o700)
    results = {}
    for name, version in WORKERS.items():
        record = metadata["workers"][name]
        snapshot = output / (name + "-inputs")
        snapshot.mkdir(mode=0o700)
        for filename, data in snapshots[name].items():
            write(snapshot / filename, data)
        core = read_file(release / "artifacts" / metadata["wheel"], LIMIT)
        if digest(core) != metadata["wheel_sha256"]:
            raise ValueError("Core wheel changed before worker install")
        write(snapshot / metadata["wheel"], core)
        environment = output / name
        python = environment / "bin/python"
        run_command(["uv", "venv", "--python", version, str(environment)], log_path=output / (name + "-venv.log"))
        run_command(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "-r",
                str(snapshot / record["requirements"]),
            ],
            timeout=300,
            log_path=output / (name + "-dependencies.log"),
        )
        run_command(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(snapshot / metadata["wheel"]),
                str(snapshot / record["wheel"]),
            ],
            timeout=60,
            log_path=output / (name + "-wheel.log"),
        )
        proof = json.loads(
            run_command(
                [str(python), "-I", "-c", PROBE, name, metadata["wheel"].split("-")[1]],
                timeout=60,
                log_path=output / (name + "-probe.log"),
            )
        )
        for option in ("catalog", "release-manifest"):
            exported = run_command(
                [str(python), "-I", "-m", "weave_" + name + "_worker.main", "--" + option],
                timeout=60,
                log_path=output / (name + "-" + option + ".json"),
            )
            if not isinstance(json.loads(exported), dict):
                raise ValueError("Worker catalog export is not an object")
        extracted = extract_sdist(snapshots[name][record["sdist"]], output / (name + "-sdist"))
        derived = output / (name + "-derived")
        run_command(
            ["uv", "build", "--wheel", "--project", str(extracted), "--out-dir", str(derived)],
            timeout=180,
            log_path=output / (name + "-sdist-build.log"),
        )
        wheels = list(derived.glob("*.whl"))
        if len(wheels) != 1 or worker_wheel(read_file(wheels[0], LIMIT), wheels[0].name, name) != worker_wheel(
            snapshots[name][record["wheel"]], record["wheel"], name
        ):
            raise ValueError("Worker sdist-derived package differs")
        results[name] = {**proof, "sdist_package_members_equal": True}
    write(output / "verification.json", (json.dumps(results, sort_keys=True, indent=2) + "\n").encode())
    return results


def build(release: Path, output: Path, context: str) -> dict:
    if context != "colima-weave-tests":
        raise ValueError("Owned release Docker context required")
    metadata = json.loads(read_file(release / "release.json", 1024 * 1024))
    validate(release, metadata)
    docker = ["docker", "--context", context]
    endpoint = json.loads(run_command(docker + ["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise ValueError("Local Docker context required")
    output.mkdir(mode=0o700)
    results = {}
    for name in WORKERS:
        receipt = output / (name + ".iid")
        run_command(
            docker + ["build", "--progress=plain", "--iidfile", str(receipt), str(release / "worker-images" / name)],
            timeout=1200,
            limit=4 * 1024 * 1024,
            log_path=output / (name + ".log"),
        )
        image = read_file(receipt, 100).decode().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Immutable worker image ID required")
        details = json.loads(run_command(docker + ["image", "inspect", image]))[0]
        if details["Id"] != image or details["Config"]["User"] != "65532:65532":
            raise ValueError("Worker image identity or user policy differs")
        json.loads(
            run_command(
                docker + ["run", "--rm", "--network", "none", "--read-only", image, "--catalog"],
                timeout=60,
                log_path=output / (name + "-catalog.json"),
            )
        )
        results[name] = image
    write(output / "images.json", (json.dumps(results, sort_keys=True, indent=2) + "\n").encode())
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify", "build"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--core-wheel")
    parser.add_argument("--context")
    args = parser.parse_args()
    try:
        release = real_path(args.release)
        if args.mode == "prepare":
            result = prepare(real_path(args.root), release, args.core_wheel)
        elif args.mode == "verify":
            result = verify(release, real_path(args.output))
        else:
            result = build(release, real_path(args.output), args.context)
    except Exception:
        parser.exit(1, "Independent worker release check failed; inspect private evidence.\n")
    print(json.dumps(result, sort_keys=True))
