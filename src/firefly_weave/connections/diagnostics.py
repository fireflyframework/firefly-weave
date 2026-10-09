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

"""Typed WV-CONNECTION diagnostics with JSON pointers into the connection request.

Every rejection keeps status 422 and code WV-CONNECTION. Diagnostics carry fixed, plain
messages: no secret values, and for secret handles one generic message at the slot pointer so a
response never reveals which handles exist or why one is unavailable.
"""

from collections.abc import Iterable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ScopedSecrets
from firefly_weave.contracts import agentic
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import (
    ConnectionInvalid,
    ConnectionIssue,
    ConnectionIssueCode,
    ConnectionRequest,
    ConnectionRevision,
)
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError

MESSAGE = "Connection requirements are unavailable or incompatible"
SECRET_UNAVAILABLE = "This secret handle is not available in this environment."
CONNECTOR_UNAVAILABLE = "Choose a published, active Connector version for this project."
ADAPTER_UNAVAILABLE = "This connector is not installed in this environment."
DIGEST_CHANGED = "The published Connector no longer matches this connection; create a new revision."
ORIGIN_ONLY = "List only an origin such as https://api.example.com: no path, query, fragment, credentials or wildcards."


def diagnostic(issue: ConnectionIssue) -> Diagnostic:
    return Diagnostic(
        code="WV-CONNECTION-" + issue.code,
        severity="error",
        stage="semantic",
        path=issue.path,
        message=issue.message,
    )


def rejected(issues: Iterable[ConnectionIssue]) -> CatalogError:
    """One 422 WV-CONNECTION problem whose ``diagnostics`` explain each rejected field."""
    unique: list[ConnectionIssue] = []
    for issue in issues:
        if not any(item.path == issue.path and item.code == issue.code for item in unique):
            unique.append(issue)
    return CatalogError(
        422,
        "WV-CONNECTION",
        MESSAGE,
        result={"diagnostics": [diagnostic(issue).model_dump(mode="json", by_alias=True) for issue in unique]},
    )


def connector_unavailable() -> CatalogError:
    return rejected([ConnectionIssue("/connector_version_id", CONNECTOR_UNAVAILABLE, "CONNECTOR")])


def _schema_issues(
    schema: JsonObject, value: object, bundle: dict[str, JsonObject], base: str, *, credentials: bool = False
) -> list[ConnectionIssue]:
    found = validate_payload(schema, cast(Any, value), bundle, credential_references=credentials)
    label = "secret handle slots" if credentials else "configuration"
    code: ConnectionIssueCode = "AUTH" if credentials else "CONFIG"
    message = f"This value does not match the connector's {label} schema."
    # Schema findings have fixed messages and instance pointers; nothing else is copied.
    return [
        ConnectionIssue(
            base + item.path,
            message if item.code == "WV-SCHEMA-INVALID_INSTANCE" else f"{message} {item.message}",
            code,
        )
        for item in found
    ]


def destination_issues(adapter: str, destinations: Iterable[str]) -> list[ConnectionIssue]:
    """Literal destination origins; wildcards, credentials and paths never broaden egress."""
    found = []
    for index, destination in enumerate(destinations):
        pointer = f"/allowed_destinations/{index}"
        if adapter == "weave-kafka":
            from firefly_weave.connectors.broker import destination as broker_destination

            try:
                broker_destination(destination)
            except ValueError:
                found.append(
                    ConnectionIssue(
                        pointer, "Use a canonical broker address such as kafka://broker:9092.", "DESTINATION"
                    )
                )
            continue
        if adapter == "weave-postgresql":
            from firefly_weave.connectors.postgresql import destination as postgres_destination

            try:
                postgres_destination(destination)
            except ValueError:
                found.append(
                    ConnectionIssue(
                        pointer, "Use a PostgreSQL address such as postgresql://10.0.0.5:5432.", "DESTINATION"
                    )
                )
            continue
        try:
            parsed = urlsplit(destination)
            port = parsed.port
        except ValueError:
            found.append(ConnectionIssue(pointer, ORIGIN_ONLY, "DESTINATION"))
            continue
        if (
            parsed.scheme not in {"https", "http"}
            or port == 0
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or "*" in destination
        ):
            found.append(ConnectionIssue(pointer, ORIGIN_ONLY, "DESTINATION"))
    return found


AGENTIC_ADAPTER = "weave-agentic-provider"


def keyless_connection(adapter: str, config: Mapping[str, object], references: Mapping[str, str]) -> bool:
    """Agentic connections on an approved local model endpoint use the reserved no-credential handle."""
    return adapter == AGENTIC_ADAPTER and agentic.keyless(config, references)


def secret_issues(
    scope: Scope, secrets: ScopedSecrets, references: Mapping[str, str], *, keyless: bool = False
) -> list[ConnectionIssue]:
    found = []
    for slot, handle in sorted(references.items()):
        try:
            secrets.check(scope, handle, keyless=keyless)
        except CatalogError:
            found.append(ConnectionIssue(pointer_child("/secretRef", slot), SECRET_UNAVAILABLE, "SECRET"))
    return found


def request_issues(
    contract: Mapping[str, Any],
    request: ConnectionRequest,
    registry: ConnectorRegistry,
    secrets: ScopedSecrets,
    scope: Scope,
) -> list[ConnectionIssue]:
    """Every reason a create request is rejected, in a stable field order."""
    spec = contract["document"]["spec"]
    adapter = spec["adapter"]
    try:
        registry.get(adapter)
    except CatalogError:
        return [ConnectionIssue("/connector_version_id", ADAPTER_UNAVAILABLE, "CONNECTOR")]
    bundle = {
        dependency["reference"]: dependency["document"]
        for dependency in contract["artifact"]["executable"]["dependencies"]
        if dependency["kind"] == "Schema"
    }
    found = _schema_issues(spec["configSchema"], request.config, bundle, "/config")
    found += _schema_issues(spec["authSchema"], request.secret_refs, bundle, "/secretRef", credentials=True)
    found += destination_issues(adapter, request.allowed_destinations)
    if not found:
        try:
            registry.validate_connection(adapter, request)
        except ConnectionInvalid as invalid:
            found += invalid.issues
        except (ValueError, CatalogError):
            found.append(ConnectionIssue("/config", "This connector does not accept these connection settings."))
    found += secret_issues(
        scope,
        secrets,
        request.secret_refs,
        keyless=keyless_connection(adapter, request.config, request.secret_refs),
    )
    return found


def readiness_issues(
    contract: Mapping[str, Any],
    connector_digest: str,
    config: JsonObject,
    adapter: str,
    references: Mapping[str, str],
    registry: ConnectorRegistry,
    secrets: ScopedSecrets,
    scope: Scope,
) -> list[ConnectionIssue]:
    """Why a saved revision cannot be used now (Connector drift, removed adapter or handle)."""
    found = []
    if contract["definition_digest"] != connector_digest:
        found.append(ConnectionIssue("/connector_version_id", DIGEST_CHANGED, "CONNECTOR"))
    bundle = {
        d["reference"]: d["document"]
        for d in contract["artifact"]["executable"]["dependencies"]
        if d["kind"] == "Schema"
    }
    found += _schema_issues(contract["document"]["spec"]["configSchema"], config, bundle, "/config")
    try:
        registry.get(adapter)
    except CatalogError:
        found.append(ConnectionIssue("/connector_version_id", ADAPTER_UNAVAILABLE, "CONNECTOR"))
    found += secret_issues(scope, secrets, references, keyless=keyless_connection(adapter, config, references))
    return found


def is_agentic_revision(revision: ConnectionRevision) -> bool:
    return (
        revision.connector == agentic.CONNECTOR_REFERENCE
        and revision.connector_digest == agentic.AGENTIC_DESCRIPTOR.manifest.digest
    )
