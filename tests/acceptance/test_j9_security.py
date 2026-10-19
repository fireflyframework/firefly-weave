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

"""J9: security and negative paths through the API, signed in as each acceptance person.

J9.1 sends every operation added since 0.1.0a14 (and the operations behind Studio controls) as the
owner, builder, approver, operator and viewer: exactly 403 WV-FORBIDDEN where the person's roles
lack the capability, never 403 or a server error elsewhere; then a revoked person loses access.
J9.3 shows the private-origin policy on connections: the approved Acme origin works over plain HTTP
with its key, and the identity provider, the Acme service by address and a metadata address are
refused before any request reaches them. Runs only through scripts/acceptance.py (the run stage).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.catalog import RetirementRequest
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.sdk.errors import WeaveError

pytestmark = pytest.mark.acceptance

PEOPLE = ("owner", "builder", "approver", "operator", "viewer")
FORBIDDEN = (403, "WV-FORBIDDEN")
REVOKED = "revoked-j9"


async def probe(client: Any, row: Any, materialize: Any) -> tuple[int, str]:
    """Send one matrix row as the client's person; the answer's status and code (200, "ok" on success)."""
    spec = OPERATIONS[row.operation]
    body = (
        None
        if spec.request is None
        else spec.request.model_validate_json(json.dumps(materialize(dict(row.body or {}))))
    )
    query = materialize(dict(row.query)) or None
    try:
        await client.invoke(
            row.operation,
            identifier=uuid4() if row.identifier else None,
            collection=row.collection,
            body=body,
            query=query,
            idempotency_key=f"j9-{uuid4().hex}" if spec.idempotency else None,
        )
    except WeaveError as error:
        return error.status, error.code
    return 200, "ok"


def wrong(outcome: dict[str, Any]) -> bool:
    answer = (outcome["status"], outcome["code"])
    if outcome["permitted"]:
        return answer == FORBIDDEN or outcome["status"] >= 500
    return answer != FORBIDDEN


@pytest.mark.journey_step("J9.1")
async def test_j9_01_every_new_operation_answers_by_role_and_revocation_takes_effect(run):
    matrix = run.helper("acceptance_authorization")
    _, rows = matrix.load()
    scope = run.demo_scope()

    def materialize(value: Any) -> Any:
        return matrix.materialize(value, scope=scope)

    outcomes = []
    for name in PEOPLE:
        roles = run.roles(name)
        async with run.client(name) as client:
            for row in rows:
                status, code = await probe(client, row, materialize)
                permitted = matrix.holds(roles, row.capability, ROLE_CAPABILITIES)
                outcomes.append(
                    {"person": name, "operation": row.operation, "permitted": permitted, "status": status, "code": code}
                )
    (run.evidence / "authorization-j9.json").write_text(
        json.dumps(outcomes, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert [outcome for outcome in outcomes if wrong(outcome)] == []
    assert {outcome["permitted"] for outcome in outcomes} == {True, False}
    await _revocation_takes_effect(run)


async def _revocation_takes_effect(run) -> None:
    value = json.loads(
        run.require(run.platform("user", "--username", REVOKED, "--role", "viewer", "--output", "json")).stdout
    )
    run.save_person(REVOKED, value)
    async with run.client(REVOKED) as person:
        await person.invoke("run_summaries.list")
        principal = (await person.invoke("identity.read")).principal_id
        async with run.client("owner") as owner:
            bindings, cursor = [], None
            for _ in range(50):
                page = await owner.invoke(
                    "members.list", query={"limit": 100, **({"cursor": cursor} if cursor else {})}
                )
                bindings += [item for item in page.items if item.principal_id == principal and item.active]
                cursor = page.next_cursor
                if not cursor:
                    break
            assert bindings, "the person to revoke holds no active grant"
            for binding in bindings:
                await owner.invoke("members.revoke", identifier=binding.id)
        deadline = time.monotonic() + 30
        while True:
            try:
                await person.invoke("run_summaries.list")
            except WeaveError as error:
                assert (error.status, error.code) == FORBIDDEN
                return
            assert time.monotonic() < deadline, "a revoked person still reads runs after 30 seconds"
            await asyncio.sleep(1)


def egress_address(run, fragment: str) -> str:
    """The IPv4 address on the installation's egress network of the container whose name holds ``fragment``."""
    network = run.platform_state()["private_origins"]["network"]
    inspected = json.loads(run.require(run.docker("network", "inspect", network)).stdout)[0]
    for container in (inspected.get("Containers") or {}).values():
        if fragment in container["Name"]:
            return str(container["IPv4Address"]).split("/")[0]
    raise AssertionError(f"No container named like {fragment} on {network}")


async def _test_connection(owner: Any, catalog: Any, version: UUID, name: str, url: str) -> str:
    """Create and test a keyless connection; "refused at creation", or the test answer's code."""
    try:
        connection = await owner.invoke("connections.create", body=catalog.open_connection_request(name, url, version))
    except WeaveError as error:
        assert error.status == 422, (name, error.status, error.code)
        return "refused at creation"
    answer = await owner.invoke("connections.test", identifier=connection.id, body=RetirementRequest())
    return "ok" if answer.ok else answer.code


@pytest.mark.journey_step("J9.3")
async def test_j9_03_connections_reach_only_the_approved_private_origin(run):
    catalog = run.helper("acceptance_catalog")
    subnet = ipaddress.ip_network(run.platform_state()["private_origins"]["subnet"])
    identity_ip, acme_ip = egress_address(run, "keycloak"), egress_address(run, "acme-api")
    assert ipaddress.ip_address(identity_ip) in subnet and ipaddress.ip_address(acme_ip) in subnet
    key = hashlib.sha256(run.canary("acme-api-key").encode()).hexdigest()
    revision = UUID(run.private_record("catalog")["connection_revision_id"])
    before = len(run.acme_control("/_control/journal")["entries"])
    async with run.client("owner") as owner:
        approved = await owner.invoke("connections.test", identifier=revision, body=RetirementRequest())
        reached = run.acme_control("/_control/journal")["entries"][before:]
        version = await catalog.connector_version(owner)
        refused = {
            name: await _test_connection(owner, catalog, version, name, url)
            for name, url in (
                ("identity-provider-j9", f"http://{identity_ip}:8080"),
                ("acme-address-j9", f"http://{acme_ip}:8080"),
                ("metadata-j9", "http://169.254.169.254"),
            )
        }
    assert approved.ok is True and approved.encrypted is False
    assert reached and {entry["key_sha256"] for entry in reached} == {key}
    assert {name: result for name, result in refused.items() if result == "ok"} == {}
    assert len(run.acme_control("/_control/journal")["entries"]) == before + len(reached)
