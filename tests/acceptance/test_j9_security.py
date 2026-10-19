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
J9.3 shows the private-origin policy on connections by running an action through each: through the
approved Acme connection the run succeeds over plain HTTP with its key; through connections to the
identity provider by address and under its approved alias, the Acme service by address and a metadata
address, the policy refuses the request by its own rule before it leaves and the run stops at the action.
Runs only through scripts/acceptance.py (the run stage).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import random
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
# The rule each refused destination must meet (the private-origin rules). The identity provider is the control plane
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


def platform_logs(run, since: str) -> str:
    """The logs of every container of the platform since ``since`` (RFC 3339), standard output and error: the API
    and whatever process runs connector actions."""
    project = f"weave-local-{run.platform_state()['id']}"
    listed = run.require(
        run.docker("ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}")
    ).stdout.split()
    assert listed, f"no container of {project} is running"
    texts = []
    for container in listed:
        logs = run.require(run.docker("logs", "--since", since, container))
        texts.append(logs.stdout + logs.stderr)
    return "\n".join(texts)


def refusal_problems(reasons: dict[str, list[str]], expected: dict[str, str]) -> list[str]:
    """Why the records do not prove each refusal: every destination needs at least one record, one per task attempt
    (the executor may retry), and every record must name the destination's rule."""
    problems = []
    for name, reason in expected.items():
        found = reasons.get(name, [])
        if not found:
            problems.append(f"{name}: no refusal record")
        problems += [f"{name}: refused by {other}, expected {reason}" for other in sorted(set(found) - {reason})]
    return problems


def refusal_attempts(reasons: dict[str, list[str]]) -> dict[str, int]:
    """How many task attempts each destination's refusal records show."""
    return {name: len(found) for name, found in reasons.items()}


def destinations(identity_ip: str, acme_ip: str, rng: random.Random) -> dict[str, str]:
    """J9.3's private destinations. Each one by address gets a port of this run, so its refusal record (which names
    the origin) can only come from this run's call; the approved alias must stay the exact approved origin."""
    ports = rng.sample(range(20000, 30000), 3)
    return {
        "identity-provider-j9": f"http://{identity_ip}:{ports[0]}",
        "identity-alias-j9": f"http://{IDENTITY_ALIAS}:8080",
        "acme-address-j9": f"http://{acme_ip}:{ports[1]}",
        "metadata-j9": f"http://169.254.169.254:{ports[2]}",
    }


async def await_refusals(
    read: Any, origins: dict[str, str], *, timeout: float = 10, pause: float = 0.5
) -> dict[str, list[str]]:
    """The refusal reasons per destination, read again until each has a record or ``timeout`` passes: Docker
    copies container logs asynchronously."""
    deadline = time.monotonic() + timeout
    while True:
        reasons = refusal_reasons(refusal_records(read()), origins)
        if all(reasons.values()) or time.monotonic() >= deadline:
            return reasons
        await asyncio.sleep(pause)


# A run that called a refused destination stops at the action: the failed task opens an incident.
REFUSED_RUN = ("suspended", "WV-TASK-FAILED")
SETTLED = frozenset({"succeeded", "failed", "cancelled", "timed_out"})


async def settle(client: Any, run_id: UUID, *, timeout: float = 180, pause: float = 2) -> tuple[str, str | None]:
    """A run's status and incident code once it finished or stopped at an incident; fails after ``timeout``."""
    deadline = time.monotonic() + timeout
    while True:
        view = await client.invoke("runs.read", identifier=run_id)
        status, incident = view.state.status, view.state.incident
        if status in SETTLED or (status == "suspended" and incident):
            return status, incident
        assert time.monotonic() < deadline, f"run still {status} after {timeout:.0f} seconds"
        await asyncio.sleep(pause)


class ReaderActivations:
    """The reader's activation revision in the demo environment: each activation replaces the one before."""

    def __init__(self) -> None:
        self.revision: int | None = None


async def run_through(
    client: Any,
    run,
    reader: Any,
    connection_id: UUID,
    releases: dict[UUID, UUID],
    name: str,
    activations: ReaderActivations,
) -> tuple[str, str | None]:
    """Activate the reader workflow with ``connection_id`` in its slot, start one run and wait for it."""
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    catalog = run.helper("acceptance_catalog")
    activation = await client.invoke(
        "activations.create",
        body=ActivationRequest(
            version_id=reader.id,
            artifact_digest=reader.digest,
            scope=client.scope,
            connection_revision_ids={catalog.READER_SLOT: connection_id},
            connector_release_ids=releases,
        ),
        idempotency_key=f"j9-03-activate-{name}-{run.run_id}",
        revision=activations.revision,
    )
    activations.revision = activation.revision
    started = await client.invoke(
        "runs.start",
        body=StartRunRequest(activation_id=activation.id, input={}),
        idempotency_key=f"j9-03-run-{name}-{run.run_id}",
    )
    return await settle(client, started.id)


@pytest.mark.journey_step("J9.3")
async def test_j9_03_connections_reach_only_the_approved_private_origin(run):
    """People use a connection by running an action through it, so each destination gets one run of the same
    workflow: the approved Acme connection reaches Acme with its key; every private destination is refused by the
    policy's own rule before any request leaves, and the run stops at the action."""
    from firefly_weave.contracts.catalog import PublicationRequest

    catalog = run.helper("acceptance_catalog")
    subnet = ipaddress.ip_network(run.platform_state()["private_origins"]["subnet"])
    identity_ip, acme_ip = egress_address(run, "keycloak"), egress_address(run, "acme-api")
    assert ipaddress.ip_address(identity_ip) in subnet and ipaddress.ip_address(acme_ip) in subnet
    key = hashlib.sha256(run.canary("acme-api-key").encode()).hexdigest()
    approved_connection = UUID(run.private_record("catalog")["connection_revision_id"])
    releases = {UUID(version): UUID(release) for version, release in run.status()["connector_release_ids"].items()}
    targets = destinations(identity_ip, acme_ip, random.SystemRandom())
    async with run.client("owner") as owner:
        reader = await owner.invoke(
            "definitions.publish",
            collection="workflows",
            body=PublicationRequest(source=catalog.reader_workflow(), format="yaml"),
            idempotency_key=f"j9-03-reader-{run.run_id}",
        )
        before = len(run.acme_control("/_control/journal")["entries"])
        activations = ReaderActivations()
        approved = await run_through(owner, run, reader, approved_connection, releases, "approved", activations)
        reached = run.acme_control("/_control/journal")["entries"][before:]
        version = await catalog.connector_version(owner)
        connections = {
            name: (
                await owner.invoke("connections.create", body=catalog.open_connection_request(name, url, version))
            ).id
            for name, url in targets.items()
        }
        # Taken just before the refused calls, with a few seconds of slack for the Docker host's clock.
        since = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        refused_from = len(run.acme_control("/_control/journal")["entries"])
        with identity_alias(run):
            runs = {
                name: await run_through(owner, run, reader, connection, releases, name, activations)
                for name, connection in connections.items()
            }
        after = run.acme_control("/_control/journal")["entries"][refused_from:]
    origins = {name: canonical_origin(url, "http-connector") for name, url in targets.items()}
    reasons = await await_refusals(lambda: platform_logs(run, since), origins)
    (run.evidence / "private-origins-j9.json").write_text(
        json.dumps(
            {
                "approved": approved,
                "runs": runs,
                "origins": origins,
                "reasons": reasons,
                "attempts": refusal_attempts(reasons),
                "acme_after": len(after),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # The approved origin: the run succeeds over plain HTTP and Acme sees exactly the canary key's hash.
    assert approved == ("succeeded", None)
    assert reached and {entry["key_sha256"] for entry in reached} == {key}
    # Every private destination: the policy's records, one per task attempt, each with the rule that refused it;
    # the run stopped at the action, and nothing reached Acme.
    assert refusal_problems(reasons, REFUSALS) == []
    assert runs == dict.fromkeys(targets, REFUSED_RUN)
    assert after == []
