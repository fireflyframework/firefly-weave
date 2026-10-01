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
"""A distribution may expose multiple separately pinned native provider declarations."""

import json
from types import SimpleNamespace

from pyfly.container import service
from pyfly.context import ApplicationContext
from pyfly.core.config import Config

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connectors.packages import ConnectorPackage, PackageMetadata
from firefly_weave.contracts.providers import provider_schema_digest
from firefly_weave.sdk.connectors import scaffold


@service
class First:
    async def execute(self, input, context):
        return input

    async def test_connection(self, connection):
        pass


@service
class Second(First):
    pass


@service
class FirstVerifier:
    async def verify(self, source, raw_body, headers, received_at):
        return (), None

    async def challenge(self, source, query):
        return None


@service
class SecondVerifier(FirstVerifier):
    pass


def test_two_providers_in_one_distribution_keep_exact_verifier(tmp_path, monkeypatch):
    entries = []
    for name, adapter, verifier in [("first", First, FirstVerifier), ("second", Second, SecondVerifier)]:
        root = tmp_path / name
        scaffold(root, name)
        doc = json.loads((root / "connector.json").read_text())
        doc.update(
            distribution="shared-dist",
            provider=name,
            service=f"{adapter.__module__}:{adapter.__name__}",
            verifier_service=f"{verifier.__module__}:{verifier.__name__}",
            event_schemas={"message": {"type": "object"}},
        )
        declaration = ConnectorPackage(PackageMetadata(FrozenDocument.from_value(doc)), adapter, verifier)
        entries.append(
            SimpleNamespace(
                dist=SimpleNamespace(name="shared-dist", version="1.0.0"),
                name=name,
                value=f"fixture:{name}",
                load=lambda d=declaration: d,
            )
        )
    monkeypatch.setattr("firefly_weave.connections.registry.entry_points", lambda **kwargs: entries)
    registry = ConnectorRegistry(tuple(f"shared-dist:{e.name}:{e.value}" for e in entries))
    context = ApplicationContext(Config({}))
    registry.register_services(context)
    registry.resolve_services(context)
    digest = provider_schema_digest({"message": {"type": "object"}})
    assert registry.provider_verifier("shared-dist", "1.0.0", "first", digest) is context.get_bean(FirstVerifier)
    assert registry.provider_verifier("shared-dist", "1.0.0", "second", digest) is context.get_bean(SecondVerifier)


def test_pep440_distribution_pin_is_separate_from_semver_implementation(tmp_path, monkeypatch):
    root = tmp_path / "alpha"
    scaffold(root, "alpha")
    doc = json.loads((root / "connector.json").read_text())
    doc.update(
        distribution="shared-dist",
        distribution_version="0.1.0a1",
        provider="alpha",
        service=f"{First.__module__}:First",
        verifier_service=f"{FirstVerifier.__module__}:FirstVerifier",
        event_schemas={"message": {"type": "object"}},
    )
    declaration = ConnectorPackage(PackageMetadata(FrozenDocument.from_value(doc)), First, FirstVerifier)
    entry = SimpleNamespace(
        dist=SimpleNamespace(name="shared-dist", version="0.1.0a1"),
        name="alpha",
        value="fixture:alpha",
        load=lambda: declaration,
    )
    monkeypatch.setattr("firefly_weave.connections.registry.entry_points", lambda **kwargs: [entry])
    registry = ConnectorRegistry(("shared-dist:alpha:fixture:alpha",))
    context = ApplicationContext(Config({}))
    registry.register_services(context)
    registry.resolve_services(context)
    digest = provider_schema_digest({"message": {"type": "object"}})
    assert registry.provider_package("shared-dist", "0.1.0a1", "alpha", digest).metadata.model.version == "1.0.0"
    assert registry.provider_verifier("shared-dist", "0.1.0a1", "alpha", digest, "1.0.0") is context.get_bean(
        FirstVerifier
    )


def test_same_provider_multiple_implementations_require_explicit_adapter_pin(tmp_path, monkeypatch):
    import pytest

    from firefly_weave.definitions.models import CatalogError

    entries = []
    for name, adapter, verifier, version in [
        ("first", First, FirstVerifier, "1.0.0"),
        ("second", Second, SecondVerifier, "2.0.0"),
    ]:
        root = tmp_path / name
        scaffold(root, name)
        doc = json.loads((root / "connector.json").read_text())
        doc.update(
            distribution="shared-dist",
            distribution_version="0.1.0a1",
            version=version,
            provider="same",
            service=f"{adapter.__module__}:{adapter.__name__}",
            verifier_service=f"{verifier.__module__}:{verifier.__name__}",
            event_schemas={"message": {"type": "object"}},
        )
        for binding in doc["bindings"]:
            binding["implementation_version"] = version
        declaration = ConnectorPackage(PackageMetadata(FrozenDocument.from_value(doc)), adapter, verifier)
        entries.append(
            SimpleNamespace(
                dist=SimpleNamespace(name="shared-dist", version="0.1.0a1"),
                name=name,
                value=f"fixture:{name}",
                load=lambda d=declaration: d,
            )
        )
    monkeypatch.setattr("firefly_weave.connections.registry.entry_points", lambda **kwargs: entries)
    registry = ConnectorRegistry(tuple(f"shared-dist:{e.name}:{e.value}" for e in entries))
    context = ApplicationContext(Config({}))
    registry.register_services(context)
    registry.resolve_services(context)
    digest = provider_schema_digest({"message": {"type": "object"}})
    with pytest.raises(CatalogError):
        registry.provider_package("shared-dist", "0.1.0a1", "same", digest)
    assert registry.provider_verifier("shared-dist", "0.1.0a1", "same", digest, "2.0.0") is context.get_bean(
        SecondVerifier
    )


def test_dispatch_metadata_offline_rejects_invalid_subsets(tmp_path):
    import pytest

    scaffold(tmp_path / "dispatch", "dispatch")
    doc = json.loads((tmp_path / "dispatch" / "connector.json").read_text())
    doc.update(event_schemas={"message": {"type": "object"}}, verifier_service="fixture:Verifier")
    for kinds in ([], ["message", "message"], ["unknown"]):
        with pytest.raises(ValueError):
            PackageMetadata(FrozenDocument.from_value({**doc, "dispatch_event_kinds": kinds}))
    assert PackageMetadata(
        FrozenDocument.from_value({**doc, "dispatch_event_kinds": ["message"]})
    ).model.dispatch_event_kinds == ["message"]
