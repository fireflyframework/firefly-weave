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

"""Only exact operator-allowlisted installed entry points may execute code."""

from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, cast

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connections.models import unavailable
from firefly_weave.connectors.builtin_catalog import BUILTIN_DECLARATIONS, BuiltinPackageMetadata
from firefly_weave.connectors.packages import ConnectorPackage, PackageMetadata
from firefly_weave.contracts.connectors import ConnectorAdapter
from firefly_weave.definitions.models import CatalogError

if TYPE_CHECKING:
    from pyfly.context import ApplicationContext

    from firefly_weave.compiler.action_config import ActionConfigValidator
    from firefly_weave.connectors.descriptor import ConnectorDescriptor
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.workers import ReleaseRequest


class ConnectorRegistry:
    def __init__(self, allowlist: tuple[str, ...] = ()) -> None:
        self._operational_guard: Callable[[], bool] | None = None
        self._adapters: dict[str, ConnectorAdapter] = {}
        self._retired: set[str] = set()
        self._descriptors: dict[str, ConnectorDescriptor] = {}
        self._packages: tuple[ConnectorPackage, ...] = ()
        self._provider_verifiers: dict[tuple[str, str, str | None, str, str], Any] = {}
        if len(set(allowlist)) != len(allowlist):
            raise ValueError("Duplicate connector allowlist identity")
        installed = entry_points(group="firefly_weave.connectors") if allowlist else ()
        selected = []
        for identity in allowlist:
            matches = [ep for ep in installed if f"{ep.dist.name if ep.dist else ''}:{ep.name}:{ep.value}" == identity]
            if len(matches) != 1:
                raise ValueError("Allowlisted connector entry point is unavailable or ambiguous")
            selected.append(matches[0])
        declarations = []
        adapters: set[str] = set()
        service_types: set[type] = set()
        for ep in selected:
            try:
                declaration = ep.load()
            except ImportError:
                raise ValueError(
                    "Enabled connector dependency is unavailable; install the operator-selected extra"
                ) from None
            if not isinstance(declaration, ConnectorPackage):
                raise ValueError(
                    "Legacy connector factories are unsupported; export a ConnectorPackage with @service types"
                )
            builtin = BUILTIN_DECLARATIONS.get(ep.name)
            selected_builtin = (
                builtin is not None
                and ep.dist is not None
                and ep.dist.name == "firefly-weave"
                and ep.value == builtin.entry_point
            )
            if selected_builtin:
                if type(declaration.metadata) is not BuiltinPackageMetadata:
                    raise ValueError("Invalid first-party declaration")
            elif type(declaration.metadata) is not PackageMetadata:
                raise ValueError("Reserved declaration requires the code-owned first-party catalog")
            doc = declaration.metadata.model
            if (
                ep.dist is None
                or doc.distribution != ep.dist.name
                or (doc.distribution_version or doc.version) != ep.dist.version
            ):
                raise ValueError("Installed package distribution/version differs from its declaration")
            if ep.name != doc.manifest.spec.adapter or ep.name in adapters:
                raise ValueError("Duplicate or mismatched adapter identity")
            types = [declaration.service_type]
            if declaration.verifier_service_type is not None:
                types.append(declaration.verifier_service_type)
            if len(set(types)) != len(types) or service_types.intersection(types):
                raise ValueError("Duplicate package service declaration")
            service_types.update(types)
            adapters.add(ep.name)
            declarations.append(declaration)
        provider_ids = [self.provider_identity(p) for p in declarations if p.verifier_service_type is not None]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Ambiguous provider declaration identity")
        self._packages = tuple(declarations)

    @property
    def packages(self) -> tuple[ConnectorPackage, ...]:
        return self._packages

    def register_services(self, context: "ApplicationContext") -> None:
        """Register selected service classes before native application startup."""
        for declaration in self._packages:
            if type(declaration.metadata) is BuiltinPackageMetadata:
                builtin = BUILTIN_DECLARATIONS[declaration.metadata.model.manifest.spec.adapter]
                for reference in builtin.supporting_services:
                    module, name = reference.split(":")
                    supporting = getattr(import_module(module), name)
                    if vars(supporting).get("__pyfly_stereotype__") != "service":
                        raise ValueError("Supporting service must be native")
                    context.register_bean(supporting)
            context.register_bean(declaration.service_type)
            if declaration.verifier_service_type is not None:
                context.register_bean(declaration.verifier_service_type)

    def resolve_services(self, context: "ApplicationContext") -> None:
        """Resolve native singletons after startup; never invoke a manual factory."""
        for declaration in self._packages:
            self.register_descriptor(declaration.descriptor, context.get_bean(declaration.service_type))
            if declaration.verifier_service_type is not None:
                self._provider_verifiers[self.provider_identity(declaration)] = context.get_bean(
                    declaration.verifier_service_type
                )

    def register_trusted(self, reference: str, adapter: ConnectorAdapter) -> None:
        """Explicit operator/test composition only; never exposed as an API operation."""
        CatalogSnapshot.from_definitions([], adapters=[reference])
        if reference in self._adapters:
            raise ValueError("Adapter identity is already registered")
        if not callable(getattr(adapter, "execute", None)) or not callable(getattr(adapter, "test_connection", None)):
            raise ValueError("Invalid connector adapter")
        self._adapters[reference] = adapter

    def set_operational_guard(self, guard: Callable[[], bool]) -> None:
        self._operational_guard = guard

    def require_operational(self) -> None:
        if self._operational_guard is not None and not self._operational_guard():
            raise CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable")

    def retire(self, reference: str) -> None:
        self._retired.add(reference)

    def get(self, reference: str) -> ConnectorAdapter:
        if reference not in self._adapters or reference in self._retired:
            raise unavailable()
        return self._adapters[reference]

    def snapshot(self) -> CatalogSnapshot:
        return CatalogSnapshot.from_definitions([], adapters=sorted(self._adapters.keys() - self._retired))

    def register_descriptor(self, descriptor: "ConnectorDescriptor", adapter: ConnectorAdapter) -> None:
        reference = str(cast(dict[str, Any], descriptor.manifest.value["spec"])["adapter"])
        self.register_trusted(reference, adapter)
        self._descriptors[reference] = descriptor

    def descriptor(self, adapter: str) -> "ConnectorDescriptor":
        self.get(adapter)
        if adapter not in self._descriptors:
            raise unavailable()
        return self._descriptors[adapter]

    def descriptors(self) -> dict[str, "ConnectorDescriptor"]:
        """Installed, non-retired descriptors by adapter; trusted read-only deployment facts."""
        return {
            adapter: descriptor
            for adapter, descriptor in sorted(self._descriptors.items())
            if adapter in self._adapters and adapter not in self._retired
        }

    def action_validators(self) -> dict[str, "ActionConfigValidator"]:
        """Compile-time Action checks keyed by the exact installed manifest digest."""
        return {
            descriptor.manifest.digest: descriptor.validate_action_config
            for descriptor in self.descriptors().values()
            if descriptor.validate_action_config is not None
        }

    def validate_manifest(self, document: dict[str, Any]) -> None:
        from firefly_weave.compiler.catalog import FrozenDocument

        adapter = document["spec"]["adapter"]
        if (
            adapter in self._descriptors
            and FrozenDocument.from_value(document).digest != self.descriptor(adapter).manifest.digest
        ):
            raise unavailable()

    def validate_release(self, request: "ReleaseRequest") -> None:
        from firefly_weave.compiler.catalog import CatalogSnapshot

        bindings = request.connector_bindings
        refs = [b.task_reference for b in bindings]
        if len(set(refs)) != len(refs):
            raise unavailable()
        offered = CatalogSnapshot.from_definitions([], tasks=request.capabilities)
        reserved = {
            f"{c.task_type}@{c.task_version}"
            for c in request.capabilities
            if c.task_type.startswith("weave-connector-")
        }
        if reserved != set(refs):
            raise unavailable()
        for binding in bindings:
            descriptor = self.descriptor(binding.adapter)
            if binding not in descriptor.bindings:
                raise unavailable()
            expected = CatalogSnapshot.from_definitions([], tasks=descriptor.capabilities).resolve(
                "TaskCapability", binding.task_reference
            )
            actual = offered.resolve("TaskCapability", binding.task_reference)
            if expected is None or actual is None or expected.digest != actual.digest:
                raise unavailable()

    def validate_connection(self, adapter: str, request: "ConnectionRequest") -> None:
        descriptor = self._descriptors.get(adapter)
        if descriptor is not None and descriptor.validate_connection is not None:
            descriptor.validate_connection(request)

    @staticmethod
    def provider_identity(package: ConnectorPackage) -> tuple[str, str, str | None, str, str]:
        from firefly_weave.contracts.providers import provider_schema_digest

        doc = package.metadata.model
        return (
            doc.distribution,
            doc.distribution_version or doc.version,
            doc.provider,
            provider_schema_digest(doc.event_schemas, doc.dispatch_event_kinds),
            doc.version,
        )

    def provider_package(
        self, distribution: str, version: str, provider: str, schema_digest: str, adapter_version: str | None = None
    ) -> ConnectorPackage:
        """A distribution may expose multiple independently pinned provider declarations."""
        matches = [
            package
            for package in self.packages
            if self.provider_identity(package)[:4] == (distribution, version, provider, schema_digest)
            and (adapter_version is None or package.metadata.model.version == adapter_version)
        ]
        if len(matches) != 1:
            raise unavailable()
        self.get(matches[0].metadata.model.manifest.spec.adapter)
        return matches[0]

    def provider_verifier(
        self, distribution: str, version: str, provider: str, schema_digest: str, adapter_version: str | None = None
    ) -> Any:
        """Resolve only already allowlisted native services with exact immutable schema pins."""
        from firefly_weave.providers.ports import ProviderVerifier

        package = self.provider_package(distribution, version, provider, schema_digest, adapter_version)
        verifier = self._provider_verifiers.get(self.provider_identity(package))
        if not isinstance(verifier, ProviderVerifier):
            raise unavailable()
        return verifier

    def builtin_verifier(self, adapter: str) -> Any:
        """Return the selected native first-party verifier; disabled providers fail closed."""
        expected = BUILTIN_DECLARATIONS.get(adapter)
        if expected is None:
            raise CatalogError(409, "WV-PROVIDER-UNAVAILABLE", "Provider is not enabled")
        matches = [
            package
            for package in self.packages
            if type(package.metadata) is BuiltinPackageMetadata
            and package.metadata.model.manifest.spec.adapter == adapter
        ]
        if len(matches) != 1:
            raise CatalogError(409, "WV-PROVIDER-UNAVAILABLE", "Provider is not enabled")
        package = matches[0]
        package.metadata.validate_identity(package.metadata.model)
        distribution, version, provider, digest, implementation = self.provider_identity(package)
        if provider is None:
            raise CatalogError(409, "WV-PROVIDER-UNAVAILABLE", "Provider is not enabled")
        try:
            return self.provider_verifier(distribution, version, provider, digest, implementation)
        except CatalogError:
            raise CatalogError(409, "WV-PROVIDER-UNAVAILABLE", "Provider is not enabled") from None
