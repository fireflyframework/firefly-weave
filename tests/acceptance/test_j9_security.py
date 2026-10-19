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
owner, builder, approver, operator and viewer: exactly 403 WV-FORBIDDEN where the person's grants at
the operation's scope lack the capability, and elsewhere the operation's own answer, never an
authentication or authorization refusal or a server error; then a revoked person loses access, in the
open session and after signing in again.
J9.3 shows the private-origin policy on connections: the approved Acme origin works over plain HTTP
with its key; the identity provider by address and under its approved alias, the Acme service by
address and a metadata address are refused before any request reaches them, each by its own rule.
Runs only through scripts/acceptance.py (the run stage).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.catalog import RetirementRequest
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.private_origins import canonical_origin
from firefly_weave.sdk.errors import WeaveError

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]

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


# A permitted call passes authorization and is answered by the operation: success, or, for a row that names a new
# random ID, the operation's own "not found", conflict or validation answer. A row that names nothing random may
# also meet the object an earlier person's identical call created (409).
NAMED_NOTHING = frozenset({404, 409, 422})
ALREADY_DONE = frozenset({409})


def wrong(outcome: dict[str, Any]) -> bool:
    """True when the answer breaks the matrix: a denied call that is not exactly 403 WV-FORBIDDEN, or a permitted
    call refused by authentication, authorization, rate limits, a malformed probe or a server error."""
    status, code = outcome["status"], outcome["code"]
    if not outcome["permitted"]:
        return (status, code) != FORBIDDEN
    if 200 <= status < 300 or [status, code] in [list(answer) for answer in outcome.get("answers", [])]:
        return False
    allowed = NAMED_NOTHING if outcome["random"] else ALREADY_DONE
    return status not in allowed or not str(code).startswith("WV-")


def names_random(row: Any) -> bool:
    """The row's request names a new random ID (its path identifier, body or query)."""
    return row.identifier or "random" in json.dumps([row.body, dict(row.query)])


@pytest.mark.journey_step("J9.1")
async def test_j9_01_every_new_operation_answers_by_role_and_revocation_takes_effect(run):
    matrix = run.helper("acceptance_authorization")
    _, rows = matrix.load()
    scope = run.demo_scope()
    capabilities = tomllib.loads((ROOT / "tests/acceptance/journeys.toml").read_text(encoding="utf-8"))["capabilities"]

    def materialize(value: Any) -> Any:
        return matrix.materialize(value, scope=scope)

    outcomes = []
    for name in PEOPLE:
        grants = run.person(name)["grants"]
        async with run.client(name) as client:
            for row in rows:
                status, code = await probe(client, row, materialize)
                # Roles count at their grant's scope: an environment grant does not reach project operations.
                path = OPERATIONS[row.operation].path
                permitted = matrix.holds_at(grants, row.capability, ROLE_CAPABILITIES, path)
                outcomes.append(
                    {
                        "person": name,
                        "operation": row.operation,
                        "permitted": permitted,
                        "random": names_random(row),
                        "answers": sorted(matrix.accepted(row, capabilities)),
                        "status": status,
                        "code": code,
                    }
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
                break
            assert time.monotonic() < deadline, "a revoked person still reads runs after 30 seconds"
            await asyncio.sleep(1)
    # Signing in again does not bring the grant back.
    async with run.client(REVOKED) as fresh:
        with pytest.raises(WeaveError) as refused:
            await fresh.invoke("run_summaries.list")
    assert (refused.value.status, refused.value.code) == FORBIDDEN


def egress_address(run, fragment: str) -> str:
    """The IPv4 address on the installation's egress network of the container whose name holds ``fragment``."""
    network = run.platform_state()["private_origins"]["network"]
    inspected = json.loads(run.require(run.docker("network", "inspect", network)).stdout)[0]
    for container in (inspected.get("Containers") or {}).values():
        if fragment in container["Name"]:
            return str(container["IPv4Address"]).split("/")[0]
    raise AssertionError(f"No container named like {fragment} on {network}")


REFUSED = '{"action":"private_origins.refused"'
IDENTITY_ALIAS = "identity.acceptance.test"
# The rule each refused destination must meet (spec: private origins). The identity provider is the control plane
# the API shares its network namespace with: by address it has no exact entry, and under its approved alias the
# control-plane rule refuses it although the entry exists. The Acme service by address has no exact entry; the
# metadata address is always refused.
REFUSALS = {
    "identity-provider-j9": "no-entry",
    "identity-alias-j9": "control-plane",
    "acme-address-j9": "no-entry",
    "metadata-j9": "always-refused",
}


@contextmanager
def identity_alias(run) -> Iterator[None]:
    """Keycloak answers to the approved identity alias on the egress network, at its own address, until exit.

    Docker adds an alias only when a container joins a network, so Keycloak leaves and rejoins at the same address.
    """
    network = run.platform_state()["private_origins"]["network"]
    inspected = json.loads(run.require(run.docker("network", "inspect", network)).stdout)[0]
    (name, address), *_ = [
        (container["Name"], str(container["IPv4Address"]).split("/")[0])
        for container in (inspected.get("Containers") or {}).values()
        if "keycloak" in container["Name"]
    ]
    run.require(run.docker("network", "disconnect", network, name))
    run.require(run.docker("network", "connect", "--ip", address, "--alias", IDENTITY_ALIAS, network, name))
    try:
        yield
    finally:
        run.require(run.docker("network", "disconnect", network, name))
        run.require(run.docker("network", "connect", "--ip", address, network, name))


def refusal_records(text: str) -> list[dict[str, Any]]:
    """Every private_origins.refused record in a log, whatever prefix the log line carries."""
    decoder = json.JSONDecoder()
    records = []
    for line in text.splitlines():
        start = line.find(REFUSED)
        if start < 0:
            continue
        try:
            record, _ = decoder.raw_decode(line[start:])
        except ValueError:
            continue
        records.append(record)
    return records


def refusal_reasons(records: list[dict[str, Any]], origins: dict[str, str]) -> dict[str, list[str]]:
    """For each named origin, the reasons the connector policy logged refusing it, in order."""
    return {
        name: [
            record["reason"]
            for record in records
            if record.get("origin") == origin and record.get("purpose") == "http-connector"
        ]
        for name, origin in origins.items()
    }


def api_logs(run, since: str) -> str:
    """The API container's log since ``since`` (RFC 3339), standard output and error."""
    project = f"weave-local-{run.platform_state()['id']}"
    listed = run.require(
        run.docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=api",
        )
    ).stdout.split()
    assert len(listed) == 1, f"expected one API container in {project}, found {len(listed)}"
    logs = run.require(run.docker("logs", "--since", since, listed[0]))
    return logs.stdout + logs.stderr


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
    # A minute of slack for the Docker host's clock; the origins below are this run's addresses.
    since = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    destinations = {
        "identity-provider-j9": f"http://{identity_ip}:8080",
        "identity-alias-j9": f"http://{IDENTITY_ALIAS}:8080",
        "acme-address-j9": f"http://{acme_ip}:8080",
        "metadata-j9": "http://169.254.169.254",
    }
    async with run.client("owner") as owner:
        approved = await owner.invoke("connections.test", identifier=revision, body=RetirementRequest())
        reached = run.acme_control("/_control/journal")["entries"][before:]
        version = await catalog.connector_version(owner)
        with identity_alias(run):
            refused = {
                name: await _test_connection(owner, catalog, version, name, url) for name, url in destinations.items()
            }
    origins = {name: canonical_origin(url, "http-connector") for name, url in destinations.items()}
    reasons = refusal_reasons(refusal_records(api_logs(run, since)), origins)
    (run.evidence / "private-origins-j9.json").write_text(
        json.dumps({"results": refused, "origins": origins, "reasons": reasons}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert approved.ok is True and approved.encrypted is False
    assert reached and {entry["key_sha256"] for entry in reached} == {key}
    assert {name: result for name, result in refused.items() if result == "ok"} == {}
    # A failed test is not a refusal: each destination needs the policy's own record, with the rule that refused it.
    assert {name: sorted(set(found)) for name, found in reasons.items()} == {
        name: [reason] for name, reason in REFUSALS.items()
    }
    assert len(run.acme_control("/_control/journal")["entries"]) == before + len(reached)
