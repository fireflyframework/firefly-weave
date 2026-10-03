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
"""Offline connector scaffolding and explicit local build/conformance operations."""

import keyword
import os
import re
import subprocess
import sys
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.parser import parse_source
from firefly_weave.connectors.packages import PackageMetadata
from firefly_weave.contracts.values import JsonObject


def weave_requirement_version() -> str:
    """The Weave version generated packages pin: the version of the code rendering them.

    ``firefly_weave.__version__`` is used rather than installed distribution
    metadata, which can describe an older install than the imported code.
    """
    from firefly_weave import __version__

    return __version__


def render_template(name: str, **values: str) -> str:
    """Render a packaged connector template; every ``__PLACEHOLDER__`` must be filled."""
    text = files("firefly_weave.sdk").joinpath("templates/connectors").joinpath(name).read_text()
    for key, value in {"WEAVE_VERSION": weave_requirement_version(), **values}.items():
        text = text.replace(f"__{key}__", value)
    if re.search(r"__[A-Z][A-Z_]*__", text):
        raise ValueError("Connector template has an unrendered placeholder")
    return text


def validate(path: Path) -> PackageMetadata:
    """Read bounded JSON only. Never import, install, or enable a package."""
    with path.open("rb") as stream:
        data = stream.read(1048577)
    if len(data) > 1048576:
        raise ValueError("Package metadata exceeds the source budget")
    return PackageMetadata(FrozenDocument.from_value(parse_source(data, format="json").value))


def scaffold(target: Path, name: str) -> None:
    """Create a pure echo package without overwriting any author-owned files."""
    if (
        not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name)
        or name.startswith("weave-")
        or keyword.iskeyword(name.replace("-", "_"))
        or len(name) > 64
    ):
        raise ValueError("Use a nonreserved lowercase package name with optional hyphens")
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Scaffold target must be absent or an empty directory")
    module = name.replace("-", "_")
    manifest: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "adapter": name,
            "configSchema": {"type": "object", "additionalProperties": False},
            "authSchema": {"type": "object", "additionalProperties": False},
            "actions": {
                "echo": {
                    "configSchema": {"type": "object", "additionalProperties": False},
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 1,
                }
            },
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 1},
        },
    }
    from firefly_weave.contracts.definitions import ConnectorDefinition

    manifest = ConnectorDefinition.model_validate(manifest).model_dump(by_alias=True)
    digest = FrozenDocument.from_value(manifest).digest
    document: JsonObject = {
        "format": "weave/connector-package-v1",
        "distribution": name,
        "version": "1.0.0",
        "family": "custom",
        "protocol_versions": ["1.0.0"],
        "service": f"{module}:Echo",
        "manifest": manifest,
        "capabilities": [
            {
                "taskType": f"weave-connector-{name}-echo",
                "taskVersion": "1.0.0",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "sideEffect": "read_only",
                "timeoutSeconds": 1,
            }
        ],
        "bindings": [
            {
                "connector_digest": digest,
                "action": "echo",
                "adapter": name,
                "implementation_version": "1.0.0",
                "task_reference": f"weave-connector-{name}-echo@1.0.0",
            }
        ],
    }
    metadata = PackageMetadata(FrozenDocument.from_value(document))
    artifacts = {
        "pyproject.toml": "pyproject.toml.tmpl",
        "README.md": "README.md.tmpl",
        f"src/{module}/__init__.py": "adapter.py.tmpl",
        f"src/{module}/conformance.py": "conformance.py.tmpl",
        "tests/test_conformance.py": "test_conformance.py.tmpl",
        "examples/provider_verifier.py": "provider_verifier.py.tmpl",
    }
    content = {path: render_template(source, NAME=name, MODULE=module) for path, source in artifacts.items()}
    from firefly_weave.compiler.canonical import canonical_bytes

    action: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": f"{name}-echo", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": f"{name}@1.0.0", "action": "echo", "config": {}},
            "sideEffect": "read_only",
            "timeoutSeconds": 1,
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "connection": {"connector": f"{name}@1.0.0"},
        },
    }
    content["examples/action.json"] = canonical_bytes(action).decode() + "\n"
    content["connector.json"] = metadata.canonical.canonical.decode() + "\n"
    content[f"src/{module}/connector.json"] = content["connector.json"]
    target.mkdir(parents=True, exist_ok=True)
    for path, value in content.items():
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as stream:
            stream.write(value)


def package(project: Path, output: Path) -> None:
    """Build an explicitly selected trusted local project; this executes its build backend."""
    if (project / ".weave-import-incomplete").exists():
        raise ValueError("Incomplete imported package cannot be built")
    if not project.is_dir() or not (project / "pyproject.toml").is_file():
        raise ValueError("Select a local project containing pyproject.toml")
    metadata = validate(project / "connector.json")
    with (project / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    doc = metadata.model
    module = doc.service.split(":")[0]
    resource = project / "src" / Path(*module.split(".")) / "connector.json"
    if validate(resource).canonical.digest != metadata.canonical.digest:
        raise ValueError("Packaged metadata resource differs from connector.json")
    expected_entry = {doc.manifest.spec.adapter: f"{module}:package"}
    declaration = config.get("project", {})
    if (
        declaration.get("name") != doc.distribution
        or declaration.get("version") != (doc.distribution_version or doc.version)
        or declaration.get("entry-points", {}).get("firefly_weave.connectors") != expected_entry
    ):
        raise ValueError("Build project and package declaration differ")
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Build output must be absent or an empty directory")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output.resolve()), str(project.resolve())],
        check=True,
        capture_output=True,
        text=True,
    )


def test_installed(identity: str, *, mode: Literal["fixtures", "native"] = "fixtures") -> dict[str, Any]:
    """Load one trusted installed declaration and resolve it through a native context.

    Fixtures execute only when selected; they are operator-trusted code, not a sandbox.
    Provider-specific live verification remains a separate explicit gate.
    """
    import asyncio

    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    from firefly_weave.connections.registry import ConnectorRegistry

    registry = ConnectorRegistry((identity,))
    declaration = registry.packages[0]
    if mode not in {"fixtures", "native"}:
        raise ValueError("Unknown conformance mode")
    if mode == "fixtures" and declaration.conformance is None:
        raise ValueError("Installed connector does not declare fixture conformance tests")
    context = ApplicationContext(Config({}))
    registry.register_services(context)
    if declaration.metadata.model.family == "http":
        from pyfly.client.ports.outbound import BoundedHttpClientPort

        from firefly_weave.connectors.egress import SecureHttpClient
        from firefly_weave.connectors.http import HttpPolicy
        from firefly_weave.connectors.http_profiles import register_http_profile_services

        context.container.register_instance(BoundedHttpClientPort, SecureHttpClient())
        context.container.register_instance(HttpPolicy, HttpPolicy())
        register_http_profile_services(context)

    async def run() -> None:
        try:
            await context.start()
            registry.resolve_services(context)
            if mode == "fixtures" and declaration.conformance is not None:
                await declaration.conformance(context.get_bean(declaration.service_type))
        finally:
            await context.stop()

    asyncio.run(run())
    descriptor = declaration.descriptor
    return {
        "mode": "installed-fixture-contract" if mode == "fixtures" else "installed-native-contract",
        "identity": identity,
        "manifest_digest": descriptor.manifest.digest,
        "package_digest": declaration.metadata.canonical.digest,
        "verification": "contract-tested" if mode == "fixtures" else "implemented",
        "live_provider_verification": False,
    }


def scaffold_import(target: Path, result: Any) -> None:
    """Write reviewed imported data and a fixed native wrapper; never execute imported text."""
    from firefly_weave.compiler.canonical import canonical_bytes
    from firefly_weave.sdk.openapi_import import OpenAPIImportResult

    if not isinstance(result, OpenAPIImportResult) or not result.ok or result.package is None:
        raise ValueError("A successful reviewed import is required")
    metadata = PackageMetadata(FrozenDocument.from_value(result.package))
    doc = metadata.model
    name = doc.distribution
    module = name.replace("-", "_")
    if (
        len(name) > 64
        or name.startswith("weave-")
        or keyword.iskeyword(module)
        or doc.service != f"{module}:ImportedHttpConnector"
        or doc.family != "http"
    ):
        raise ValueError("Invalid HTTP package identity")
    content = {
        f"src/{module}/__init__.py": render_template("http_adapter.py.tmpl", NAME=name, MODULE=module),
        "pyproject.toml": render_template("pyproject.toml.tmpl", NAME=name, MODULE=module).replace(
            'version = "1.0.0"', f'version = "{doc.version}"'
        ),
        "README.md": "# Imported HTTP connector\n\nReview connector.json and examples before building. "
        "Installation and operator allowlisting are separate. Run connector test with --mode native; "
        "this scaffold has no live-provider certification. HTTP profile version: 2.0.0.\n",
        "import-provenance.json": canonical_bytes(result.provenance or {}).decode() + "\n",
    }
    data = metadata.canonical.canonical.decode() + "\n"
    content[f"src/{module}/connector.json"] = data
    from firefly_weave.contracts.definitions import ActionDefinition

    for action in result.actions:
        action_name = ActionDefinition.model_validate(action).metadata.name
        content[f"examples/{action_name}.action.json"] = canonical_bytes(action).decode() + "\n"
    content["connector.json"] = data
    _write_import_files(target, content)


def scaffold_builtin(target: Path, result: Any) -> list[str]:
    """Write reviewed Actions for the built-in ``weave-http@2.0.0`` connector; no code, no package.

    Writes ``actions/<name>.action.json``, ``connection.example.json`` (secret
    handle placeholders, never values) and ``provenance.json`` into an absent or
    empty directory, with the same anchored no-follow writer as package imports.
    Returns the relative paths written.
    """
    from firefly_weave.compiler.canonical import canonical_bytes
    from firefly_weave.contracts.definitions import ActionDefinition
    from firefly_weave.sdk.openapi_import import OpenAPIImportResult

    if (
        not isinstance(result, OpenAPIImportResult)
        or not result.ok
        or result.package is not None
        or result.connection_example is None
        or not result.actions
    ):
        raise ValueError("A successful reviewed built-in import is required")
    content: dict[str, str] = {}
    for action in result.actions:
        action_name = ActionDefinition.model_validate(action).metadata.name
        content[f"actions/{action_name}.action.json"] = canonical_bytes(action).decode() + "\n"
    content["connection.example.json"] = canonical_bytes(result.connection_example).decode() + "\n"
    content["provenance.json"] = canonical_bytes(result.provenance or {}).decode() + "\n"
    _write_import_files(target, content, final="provenance.json")
    return sorted(content)


def _write_import_files(target: Path, content: dict[str, str], *, final: str = "pyproject.toml") -> None:
    """Anchor all directory components; never reopen destinations through mutable paths."""
    target = target.absolute()
    if ".." in target.parts or target == Path(target.anchor):
        raise ValueError("Invalid scaffold target")
    handles: list[int] = []
    links: list[tuple[int, str, int]] = []
    directories: dict[str, int] = {}
    expected: dict[int, set[str]] = {}
    file_states: dict[tuple[int, str], os.stat_result] = {}

    def check() -> None:
        for parent, name, descriptor in links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            anchored = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (anchored.st_dev, anchored.st_ino):
                raise ValueError("Scaffold directory was replaced")
        for (parent, name), previous in file_states.items():
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if any(
                getattr(current, field) != getattr(previous, field)
                for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            ):
                raise ValueError("Scaffold file was replaced or modified")
        for descriptor, names in expected.items():
            if set(os.listdir(descriptor)) != names:
                raise ValueError("Scaffold directory was contaminated")

    def descend(parent: int, name: str, *, exclusive: bool = False) -> int:
        check()
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            if exclusive:
                raise
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        handles.append(descriptor)
        links.append((parent, name, descriptor))
        if parent in expected:
            expected[parent].add(name)
        check()
        return descriptor

    def write(path: str, value: str) -> None:
        parts = path.split("/")
        descriptor = directories[""]
        for index, part in enumerate(parts[:-1]):
            prefix = "/".join(parts[: index + 1])
            if prefix not in directories:
                directories[prefix] = descend(descriptor, part, exclusive=True)
                expected[directories[prefix]] = set()
            descriptor = directories[prefix]
        check()
        file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        expected[descriptor].add(parts[-1])
        file_states[descriptor, parts[-1]] = os.fstat(file_fd)
        try:
            check()
            with os.fdopen(file_fd, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(value)
                stream.flush()
                os.fsync(file_fd)
            file_states[descriptor, parts[-1]] = os.fstat(file_fd)
            check()
        finally:
            os.close(file_fd)

    try:
        descriptor = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(descriptor)
        for part in target.parts[1:]:
            descriptor = descend(descriptor, part)
        directories[""] = descriptor
        expected[descriptor] = set()
        check()
        write(".weave-import-incomplete", "Incomplete import; do not build or install.\n")
        for path, value in content.items():
            if path != final:
                write(path, value)
        # A build declaration (or the provenance record) is admitted only after every data file is complete.
        write(final, content[final])
        check()
        os.unlink(".weave-import-incomplete", dir_fd=descriptor)
        expected[descriptor].remove(".weave-import-incomplete")
        del file_states[descriptor, ".weave-import-incomplete"]
        check()
    except BaseException:
        if "" in directories:
            try:
                marker_fd = os.open(
                    ".weave-import-incomplete",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directories[""],
                )
                os.close(marker_fd)
            except OSError:
                pass
        raise
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)
