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
"""Installed metadata allowlisting and native container singleton composition."""

import importlib
import json
import sys

import pytest


@pytest.fixture
def package_fixture(tmp_path, monkeypatch):
    from firefly_weave.sdk.connectors import scaffold

    scaffold(tmp_path / "sample", "acme-echo")
    root = tmp_path / "sample/src"
    module = root / "acme_echo/__init__.py"
    marker = tmp_path / "imported"
    module.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "from pyfly.container import service\n"
        "from firefly_weave.connectors.packages import ConnectorPackage\n"
        "from firefly_weave.sdk.connectors import validate\n"
        "class Sentinel: pass\n"
        "@service\n"
        "class Echo:\n"
        "    def __init__(self, sentinel: Sentinel): self.sentinel = sentinel\n"
        "    async def execute(self, input, context): return input\n"
        "    async def test_connection(self, connection): pass\n"
        f"package = ConnectorPackage(validate(Path({str(tmp_path / 'sample/connector.json')!r})), Echo)\n"
    )
    document_path = tmp_path / "sample/connector.json"
    document = json.loads(document_path.read_text())
    document["service"] = "acme_echo:Echo"
    document_path.write_text(json.dumps(document))
    info = root / "acme_echo-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-echo\nVersion: 1.0.0\n")
    (info / "entry_points.txt").write_text("[firefly_weave.connectors]\nacme-echo = acme_echo:package\n")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    yield marker, module, "acme-echo:acme-echo:acme_echo:package"
    sys.modules.pop("acme_echo", None)


def test_unallowlisted_package_is_not_loaded(package_fixture):
    from firefly_weave.connections.registry import ConnectorRegistry

    marker, module, _ = package_fixture
    module.write_text("import unavailable_optional_sdk\n")
    registry = ConnectorRegistry()
    assert not marker.exists()
    assert registry.snapshot().resolve("Adapter", "acme-echo") is None


def test_allowlisted_native_singleton(package_fixture):
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    from firefly_weave.connections.registry import ConnectorRegistry

    marker, _, identity = package_fixture
    registry = ConnectorRegistry((identity,))
    assert marker.exists()
    module = importlib.import_module("acme_echo")
    context = ApplicationContext(Config({}))
    sentinel = module.Sentinel()
    context.container.register_instance(module.Sentinel, sentinel)
    registry.register_services(context)
    registry.resolve_services(context)
    assert registry.get("acme-echo") is context.get_bean(module.Echo)
    assert registry.get("acme-echo").sentinel is sentinel


def test_enabled_missing_dependency_fails_clearly(package_fixture):
    from firefly_weave.connections.registry import ConnectorRegistry

    _, module, identity = package_fixture
    module.write_text("import unavailable_optional_sdk\n")
    with pytest.raises(ValueError, match="dependency"):
        ConnectorRegistry((identity,))


def test_duplicate_allowlist_is_rejected_before_import(package_fixture):
    from firefly_weave.connections.registry import ConnectorRegistry

    marker, _, identity = package_fixture
    with pytest.raises(ValueError):
        ConnectorRegistry((identity, identity))
    assert not marker.exists()


def test_ambiguous_installed_entrypoint_is_rejected_before_load(package_fixture, monkeypatch):
    from importlib.metadata import entry_points

    from firefly_weave.connections.registry import ConnectorRegistry

    marker, _, identity = package_fixture
    entries = list(entry_points(group="firefly_weave.connectors"))
    monkeypatch.setattr("firefly_weave.connections.registry.entry_points", lambda **kw: entries + entries)
    with pytest.raises(ValueError, match="ambiguous"):
        ConnectorRegistry((identity,))
    assert not marker.exists()


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("remove-service", "@service"),
        ("factory", "Legacy"),
        ("version", "version"),
    ],
)
def test_invalid_installed_declarations_fail(package_fixture, mutation, message):
    from firefly_weave.connections.registry import ConnectorRegistry

    _, module, identity = package_fixture
    code = module.read_text()
    if mutation == "remove-service":
        code = code.replace("@service\n", "")
    elif mutation == "factory":
        code += "\npackage = lambda: Echo(None)\n"
    else:
        metadata = module.parent.parent.parent / "connector.json"
        data = json.loads(metadata.read_text())
        data["distribution"] = "other-dist"
        metadata.write_text(json.dumps(data))
    module.write_text(code)
    with pytest.raises(ValueError, match=message):
        ConnectorRegistry((identity,))


@pytest.mark.parametrize("enabled", [False, True])
async def test_native_application_boot_uses_selected_services_only(package_fixture, tmp_path, monkeypatch, enabled):
    import firefly_weave.connectors as connectors
    from firefly_weave.app import make_app
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.settings import Settings

    marker, _, identity = package_fixture
    poison = tmp_path / "optional"
    poison.mkdir()
    (poison / "unselected.py").write_text("import nonexistent_optional_provider_sdk\n")
    monkeypatch.setattr(connectors, "__path__", [*connectors.__path__, str(poison)])
    settings = Settings(
        database_url="postgresql+asyncpg://fixture@127.0.0.1:59999/unused",
        scheduler_enabled=False,
        connector_packages=(identity,) if enabled else (),
    )
    app = make_app(settings)
    context = app.state.pyfly.context
    if enabled:
        module = importlib.import_module("acme_echo")
        context.container.register_instance(module.Sentinel, module.Sentinel())
    try:
        await app.state.pyfly.startup()
        registry = context.get_bean(ConnectorRegistry)
        registry.resolve_services(context)
        if enabled:
            assert registry.get("acme-echo") is context.get_bean(module.Echo)
        else:
            assert not marker.exists()
        assert "firefly_weave.connectors.unselected" not in sys.modules
    finally:
        await app.state.pyfly.shutdown()
        await app.state.resources.close()
        for owner in app.state.telemetry:
            owner.close()


def test_installed_test_requires_explicit_fixture_suite(package_fixture):
    from firefly_weave.sdk.connectors import test_installed

    _, _, identity = package_fixture
    with pytest.raises(ValueError, match="fixture"):
        test_installed(identity)


def test_package_preserves_trusted_connection_validator(package_fixture):
    from dataclasses import replace

    from firefly_weave.connections.registry import ConnectorRegistry

    _, _, identity = package_fixture
    declaration = ConnectorRegistry((identity,)).packages[0]

    def validate_connection(request):
        raise ValueError("profile-specific denial")

    configured = replace(declaration, validate_connection=validate_connection)
    assert configured.descriptor.validate_connection is validate_connection
    assert configured.descriptor.manifest.digest == declaration.descriptor.manifest.digest


@pytest.mark.parametrize("slot", ["adapter", "verifier"])
@pytest.mark.parametrize("decoration", ["service-over-refresh", "refresh-over-service", "inherited-refresh"])
def test_rejects_effective_refresh_scope_before_instantiation(package_fixture, slot, decoration):
    from pyfly.container import Scope, refresh_scope, service
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    from firefly_weave.compiler.catalog import FrozenDocument
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connectors.packages import ConnectorPackage, PackageMetadata

    _, _, identity = package_fixture
    original = ConnectorRegistry((identity,)).packages[0]
    constructed = []

    def construct(self):
        constructed.append(self)

    async def execute(self, input, context):
        return input

    async def test_connection(self, connection):
        pass

    bases = (refresh_scope(type("RefreshBase", (), {})),) if decoration == "inherited-refresh" else ()
    candidate = type(
        "ScopeCandidate",
        bases,
        {"__module__": __name__, "__init__": construct, "execute": execute, "test_connection": test_connection},
    )
    if decoration == "service-over-refresh":
        candidate = service(refresh_scope(candidate))
    elif decoration == "refresh-over-service":
        candidate = refresh_scope(service(candidate))
    else:
        candidate = service(candidate)
        assert "__pyfly_refresh_scope__" not in vars(candidate)

    context = ApplicationContext(Config({}))
    context.register_bean(candidate)
    registration = context.container.get_registration(candidate)
    assert registration.scope == "refresh"
    assert registration.scope is not Scope.SINGLETON
    assert constructed == []

    document = original.metadata.document
    document["service" if slot == "adapter" else "verifier_service"] = f"{__name__}:ScopeCandidate"
    metadata = PackageMetadata(FrozenDocument.from_value(document))
    with pytest.raises(ValueError, match="singleton scope"):
        ConnectorPackage(
            metadata,
            candidate if slot == "adapter" else original.service_type,
            candidate if slot == "verifier" else None,
        )
    assert constructed == []
