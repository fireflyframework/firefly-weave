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

"""Prove locked installed closures and a safe sdist-derived wheel in fresh environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from firefly_weave.sdk.deployment import read_file, real_path, run_command, wheel_licenses

PROBE = r"""
import asyncio, importlib.metadata, importlib.util, json, pathlib, sys
import firefly_weave
mode = sys.argv[1]
origin = pathlib.Path(firefly_weave.__file__).resolve()
assert origin.is_relative_to(pathlib.Path(sys.prefix).resolve())
assert 'site-packages' in origin.parts
for name in ('sqlalchemy', 'asyncpg', 'alembic', 'starlette', 'uvicorn'):
    assert (importlib.util.find_spec(name) is not None) == (mode in ('server', 'teams', 'kafka'))
assert (importlib.util.find_spec('pyfly') is not None) == (mode != 'base')
assert (importlib.util.find_spec('microsoft_agents') is not None) == (mode == 'teams')
assert (importlib.util.find_spec('aiokafka') is not None) == (mode == 'kafka')
if mode != 'base':
    assert importlib.metadata.version('pyfly') == '26.9.15'
if mode == 'cli':
    import httpx, keyring
if mode == 'worker':
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker
    class Empty:
        async def claim(self, maximum):
            await worker.stop()
            return []
    worker = Worker(Empty(), {}, 1)
    asyncio.run(worker.run())
print(json.dumps({
    'closure': mode, 'python': sys.version.split()[0],
    'version': importlib.metadata.version('firefly-weave'), 'installed': str(origin),
    'packages': sorted((d.metadata['Name'], d.version) for d in importlib.metadata.distributions()),
}))
"""


def public_source_path(path: PurePosixPath) -> bool:
    """Match the checked-in product allowlist, never broad task-word substrings."""
    if set(path.parts) & {".superpowers", ".codex", ".agents", ".secrets", ".local", ".git", "AGENTS.md", "CLAUDE.md"}:
        return False
    if any(part.startswith(".env") for part in path.parts):
        return False
    if path.as_posix() == "docs/implementation-status.md" or path.is_relative_to("docs/superpowers"):
        return False
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    allowed = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    return path.as_posix() == "PKG-INFO" or any(
        path == PurePosixPath(item.lstrip("/")) or path.is_relative_to(item.lstrip("/")) for item in allowed
    )


def extract_sdist(archive: Path, target: Path) -> Path:
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        names = set()
        total = 0
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                member.name != unquote(member.name)
                or member.name.rstrip("/") != path.as_posix()
                or not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or member.name in names
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe sdist member")
            if (
                any(
                    part in {".superpowers", ".secrets", ".local", ".git"} or part.startswith(".env")
                    for part in path.parts
                )
                or "/docs/superpowers/" in member.name
            ):
                raise ValueError("Private history in sdist")
            if member.isfile() and not public_source_path(PurePosixPath(*path.parts[1:])):
                raise ValueError("Nonproduct path in sdist")
            total += member.size
            if total > 128 * 1024 * 1024:
                raise ValueError("Oversized sdist")
            names.add(member.name)
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("Expected one sdist root")
        target.mkdir(mode=0o700)
        stream.extractall(target, filter="data")
        return target / roots.pop()


def verify_cli_installer(release: Path, output: Path, metadata: dict) -> dict:
    """Exercise the shipped bootstrap, without a checkout-based Python import path."""
    artifacts = release / "artifacts"
    checksums = {}
    for line in read_file(artifacts / "SHA256SUMS", 1024 * 1024).decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)", line)
        if not match or match[2] in checksums:
            raise ValueError("Invalid CLI installer checksum inventory")
        checksums[match[2]] = match[1]
    installer = read_file(artifacts / "install.sh", 1024 * 1024)
    installer_hash = hashlib.sha256(installer).hexdigest()
    if checksums.get("install.sh") != installer_hash:
        raise ValueError("CLI installer checksum mismatch")
    manifest_bytes = read_file(artifacts / "cli-install.json", 1024 * 1024)
    if checksums.get("cli-install.json") != hashlib.sha256(manifest_bytes).hexdigest():
        raise ValueError("CLI installation manifest checksum mismatch")
    manifest = json.loads(manifest_bytes)
    if manifest.get("wheel_sha256") != metadata["wheel_sha256"]:
        raise ValueError("CLI installation wheel differs from the verified release")
    # Execute a byte-identical snapshot so a changed source cannot race the hash check.
    bootstrap = output / "verified-install.sh"
    with bootstrap.open("xb") as stream:
        stream.write(installer)
    root, bindir = output / "cli-runtime", output / "cli-bin"
    run_command(
        [
            "env",
            f"WEAVE_INSTALL_PYTHON={sys.executable}",
            "sh",
            str(bootstrap),
            "--from-release",
            str(artifacts),
            "--install-dir",
            str(root),
            "--bin-dir",
            str(bindir),
        ],
        timeout=900,
        limit=4 * 1024 * 1024,
        log_path=output / "cli-installer.log",
    )
    command = bindir / "weave"
    if not command.is_symlink():
        raise ValueError("CLI installer did not create a managed command")
    target = command.resolve(strict=True)
    environment = target.parent.parent
    if environment.parent != root / "versions" or target.name != "weave" or target.parent.name != "bin":
        raise ValueError("CLI installer command escaped the managed environment")
    python = environment / "bin/python"
    version = json.loads(
        run_command(
            [str(python), "-I", str(command), "version", "--output", "json"],
            timeout=30,
            log_path=output / "cli-version.log",
        )
    )
    if version.get("version") != manifest["version"]:
        raise ValueError("CLI installer smoke version differs from the release")
    help_output = run_command(
        [str(python), "-I", str(command), "--help"],
        timeout=30,
        log_path=output / "cli-help.log",
    )
    if b"Usage:" not in help_output or b"workflow" not in help_output:
        raise ValueError("Installed CLI help is incomplete")
    proof = json.loads(
        run_command(
            [str(python), "-I", "-c", PROBE, "cli"],
            timeout=30,
            log_path=output / "cli-probe.log",
        )
    )
    return {
        "complete": True,
        "installer_sha256": installer_hash,
        "requirements_sha256": manifest["requirements_sha256"],
        "version": version["version"],
        "command": str(command),
        "environment": str(environment),
        "closure": proof,
    }


def verify(release: Path, output: Path) -> dict:
    release, output = real_path(release), real_path(output)
    if output.exists():
        raise ValueError("Verification output must be new")
    metadata = json.loads(read_file(release / "release.json", 1024 * 1024))
    if metadata.get("complete") is not True:
        raise ValueError("Incomplete release preparation")
    for key in ("wheel", "sdist"):
        if Path(metadata[key]).name != metadata[key]:
            raise ValueError("Invalid artifact path")
        data = read_file(release / "artifacts" / metadata[key], 64 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != metadata[key + "_sha256"]:
            raise ValueError("Artifact identity mismatch")
    wheel_licenses(read_file(release / "artifacts" / metadata["wheel"], 64 * 1024 * 1024), metadata["wheel"])
    closures = ("base", "worker", "server", "teams", "kafka")
    expected = {
        metadata["wheel"],
        "Dockerfile",
        "worker.Dockerfile",
        "image_identity.py",
        "main.py",
        "manifest.json",
        "LICENSE",
        "NOTICE",
        *(name + "-requirements.txt" for name in closures),
    }
    directory = release / "images"
    if set(metadata.get("inputs", {})) != expected or {path.name for path in directory.iterdir()} != expected | {
        "release.json",
        ".dockerignore",
    }:
        raise ValueError("Prepared input inventory differs from the release contract")
    if json.loads(read_file(directory / "release.json", 1024 * 1024)) != metadata:
        raise ValueError("Prepared image receipt differs from the release identity")
    validated = {}
    for name, digest in metadata["inputs"].items():
        data = read_file(directory / name, 64 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Prepared input identity mismatch")
        if name.endswith("-requirements.txt"):
            validated[name] = data
    closure_hashes = {name: metadata["inputs"][name + "-requirements.txt"] for name in closures}
    output.mkdir(mode=0o700)
    requirements = output / "requirements"
    requirements.mkdir(mode=0o700)
    for name, data in validated.items():
        with (requirements / name).open("xb") as stream:
            stream.write(data)
    with (output / "validated-inputs.json").open("x") as stream:
        json.dump({"inputs": metadata["inputs"], "closure_sha256": closure_hashes}, stream, indent=2, sort_keys=True)
        stream.write("\n")
    results = []
    for closure in closures:
        environment = output / closure
        python = environment / "bin/python"
        run_command(["uv", "venv", "--python", "3.12", str(environment)], log_path=output / (closure + "-venv.log"))
        run_command(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "-r",
                str(requirements / (closure + "-requirements.txt")),
            ],
            timeout=180,
            limit=4 * 1024 * 1024,
            log_path=output / (closure + "-dependencies.log"),
        )
        run_command(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(release / "artifacts" / metadata["wheel"]),
            ],
            timeout=60,
            log_path=output / (closure + "-wheel.log"),
        )
        proof = run_command(
            [str(python), "-I", "-c", PROBE, closure], timeout=30, log_path=output / (closure + "-probe.log")
        )
        results.append(json.loads(proof))
    extracted = extract_sdist(release / "artifacts" / metadata["sdist"], output / "sdist")
    derived = output / "sdist-wheel"
    run_command(
        ["uv", "build", "--wheel", "--project", str(extracted), "--out-dir", str(derived)],
        timeout=180,
        limit=4 * 1024 * 1024,
        log_path=output / "sdist-build.log",
    )
    wheels = list(derived.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected one sdist-derived wheel")
    wheel_licenses(read_file(wheels[0], 64 * 1024 * 1024), wheels[0].name)
    import zipfile

    with zipfile.ZipFile(wheels[0]) as actual, zipfile.ZipFile(release / "artifacts" / metadata["wheel"]) as expected:
        expected_members = {name: expected.read(name) for name in expected.namelist() if ".dist-info/" not in name}
        actual_members = {name: actual.read(name) for name in actual.namelist() if ".dist-info/" not in name}
        if actual_members != expected_members:
            raise ValueError("Sdist-derived package members differ")
    cli_installer = verify_cli_installer(release, output, metadata)
    value = {
        "complete": True,
        "cli_installer": cli_installer,
        "wheel_sha256": metadata["wheel_sha256"],
        "sdist_sha256": metadata["sdist_sha256"],
        "closures": results,
        "closure_sha256": closure_hashes,
        "sdist_package_members_equal": True,
    }
    with os.fdopen(os.open(output / "verification.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.release, args.output)
    except Exception:
        parser.exit(1, "Artifact verification failed; inspect the bounded private stage logs.\n")
    print(json.dumps({"complete": result["complete"], "wheel_sha256": result["wheel_sha256"]}))
