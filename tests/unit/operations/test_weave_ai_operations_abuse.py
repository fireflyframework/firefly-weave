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

"""No excluded value of an operations record reaches the gateway, whatever the record holds.

Every excluded field of the fixture records carries its own canary. Text canaries are token-shaped, so a builder that
copied the field by mistake would send it past the token check and this suite would see it; every UUID field holds a
distinct fixed UUID, searched hyphenated and as hex. The positive control proves each canary was planted, and the
leak check is itself tested at every allowlisted path.
"""

import json
import re
import zlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, TypeAliasType, get_args
from uuid import UUID

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from test_weave_ai_operations import (
    NOW,
    SCOPE,
    UUID_SHAPED,
    Owners,
    ask,
    asker,
    change_request,
    incident_facts,
    incident_view,
    job,
    keys,
    plan,
    summary_facts,
    summary_of,
    worker,
)

from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.deployments import ExternalIdentity
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfiguration
from firefly_weave.contracts.workers import WorkerStatus
from firefly_weave.operations.incident_codes import INCIDENT_CODES
from firefly_weave.operations.incidents import IncidentFacts
from firefly_weave.operations.lumi_context import EXPLAIN_ONLY
from firefly_weave.operations.lumi_deployments import LIMITATION
from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings
from firefly_weave.operations.lumi_operations import (
    ATTENTION_TITLES,
    CHANGE_REQUEST_FIELDS,
    CODE_TOKEN,
    ENVIRONMENT_FIELDS,
    INCIDENT_FIELDS,
    KEY_TOKEN,
    NAME_TOKEN,
    UNKNOWN_CODE_NOTE,
    WORKER_FIELDS,
    change_request_context,
    environment_context,
    incident_context,
    worker_context,
)
from firefly_weave.operations.summary import SummaryFacts

FIELDS = {
    "worker": WORKER_FIELDS,
    "incident": INCIDENT_FIELDS,
    "environment": ENVIRONMENT_FIELDS,
    "change-request": CHANGE_REQUEST_FIELDS,
}


def uid(n: int) -> UUID:
    return UUID(f"7f3a0000-0000-4000-8000-{n:012x}")


# One token-shaped canary per excluded item, grouped as the security rules list them. Each passes the name check, so
# only the field allowlists keep it out.
TEXT = {
    # Logs: task error text and the free text a person or system wrote into a record.
    "incident resolution reason": "canary-reason-7f3a",
    "incident resolution output": "canary-output-7f3a",
    "incident resolution evidence_reference": "canary-evidence-7f3a",
    # Business payloads.
    "incident transaction business_key": "canary-business-7f3a",
    "incident transaction participant": "canary-participant-7f3a",
    "incident transaction pending": "canary-pending-7f3a",
    # Where things run.
    "worker identity host": "canary-host-7f3a.internal",
    "worker identity node": "canary-node-7f3a",
    "worker identity namespace": "canary-namespace-7f3a",
    "worker identity labels": "canary-label-7f3a",
    "worker identity os": "canary-os-7f3a",
    "worker identity arch": "canary-arch-7f3a",
    "worker identity language_version": "canary-language-7f3a",
    "change request drain member host": "canary-drain-host-7f3a",
    "change request image registry": "canary-registry-7f3a",
    "plan component configuration": "canary-configuration-7f3a",
    "job changed resource": "canary-resource-7f3a",
    # Identifiers that are not UUIDs.
    "incident dedupe handle": "canary-handle-7f3a",
    "summary api build": "sha256:" + "7f3a" * 16,
    "change request plan digest": "7f3a" * 15 + "a7f3",
    "plan digest": "7f3a" * 15 + "b7f3",
    "plan observation digest": "7f3a" * 15 + "c7f3",
    "job plan digest": "7f3a" * 15 + "d7f3",
    "plan step expected_version": "canary-version-7f3a",
    # Typed text.
    "change request title": "canary-title-7f3a",
    "change request reason": "canary-change-reason-7f3a",
    "change request failure message": "canary-failure-7f3a",
}
# Log-shaped text, planted in the same free-text fields as a second check, never the only one.
LOG_SHAPED = ("ERROR canary-log Traceback (most recent call last)", "level=error msg=canary-log")
# One fixed UUID per UUID field, each record's own ID included.
IDS = {
    "worker id": uid(1),
    "worker release_id": uid(2),
    "worker principal_id": uid(3),
    "worker placement target_id": uid(4),
    "worker placement deployment_id": uid(5),
    "incident id": uid(11),
    "incident run_id": uid(12),
    "incident actor_id": uid(13),
    "incident receipt_id": uid(14),
    "summary api instance": uid(21),
    "attention item id": uid(22),
    "approval item id": uid(23),
    "change request id": uid(31),
    "change request target_id": uid(32),
    "change request deployment_id": uid(33),
    "change request requested_by": uid(34),
    "change request planned_by": uid(35),
    "change request approval principal": uid(36),
    "change request history actor": uid(37),
    "change request worker_release_id": uid(38),
    "change request drain member": uid(39),
    "change request plan_id": uid(40),
    "change request apply_job_id": uid(41),
    "plan observation_id": uid(42),
    "plan worker_release_id": uid(43),
    "job operation_key": uid(44),
    "change request scope tenant_id": uid(51),
    "change request scope project_id": uid(52),
    "change request scope environment_id": uid(53),
    "change request observation_job_id": uid(54),
    "change request observation_id": uid(55),
    "change request drained": uid(56),
    "change request drained_revisions": uid(57),
    "plan scope tenant_id": uid(61),
    "plan scope project_id": uid(62),
    "plan scope environment_id": uid(63),
    "plan target_id": uid(64),
    "plan deployment_id": uid(65),
    "job scope tenant_id": uid(71),
    "job scope project_id": uid(72),
    "job scope environment_id": uid(73),
    "job target_id": uid(74),
    "job observation_id": uid(75),
}
PROVIDER_KEY = "canary-provider-key-7f3a"


def free_text(variant: str, name: str) -> str:
    """The token canary of a free-text field, or one of the log-shaped lines in the second check."""
    if variant == "token":
        return TEXT[name]
    return LOG_SHAPED[zlib.crc32(name.encode()) % 2]


def scope_of(record: str) -> dict[str, str]:
    return {key: str(IDS[f"{record} scope {key}"]) for key in ("tenant_id", "project_id", "environment_id")}


def canaried(variant: str = "token") -> dict[str, BaseModel]:
    """The fixture records, each excluded field holding its canary."""
    status = worker(
        id=str(IDS["worker id"]),
        release_id=str(IDS["worker release_id"]),
        principal_id=str(IDS["worker principal_id"]),
        identity={
            "host": TEXT["worker identity host"],
            "runtime": "container",
            "sdk_version": "0.1.0a17",
            "language_version": TEXT["worker identity language_version"],
            "os": TEXT["worker identity os"],
            "arch": TEXT["worker identity arch"],
            "platform_hint": "docker",
            "namespace": TEXT["worker identity namespace"],
            "node": TEXT["worker identity node"],
            "labels": {"team": TEXT["worker identity labels"]},
        },
        placement={
            "target_id": str(IDS["worker placement target_id"]),
            "target_name": "local-docker",
            "deployment_id": str(IDS["worker placement deployment_id"]),
            "component": "fixture-worker",
        },
    )
    view = incident_view(
        id=str(IDS["incident id"]),
        run_id=str(IDS["incident run_id"]),
        actor_id=str(IDS["incident actor_id"]),
        incident_key=TEXT["incident dedupe handle"],
        resolution={
            "receipt_id": str(IDS["incident receipt_id"]),
            "kind": "accept_reconciled_result",
            "reason": free_text(variant, "incident resolution reason"),
            "output": {"value": free_text(variant, "incident resolution output")},
            "evidence_reference": free_text(variant, "incident resolution evidence_reference"),
        },
        transaction={
            "instance_key": "charge",
            "state": "in_doubt",
            "business_key": TEXT["incident transaction business_key"],
            "participants": [
                {
                    "node_id": TEXT["incident transaction participant"],
                    "instance_key": TEXT["incident transaction participant"],
                    "mode": "reserve",
                    "status": "done",
                    "effect": "none",
                }
            ],
            "pending": [TEXT["incident transaction pending"]],
        },
    )
    summary = summary_of(
        api={
            "version": "0.1.0a17",
            "build": TEXT["summary api build"],
            "instance": str(IDS["summary api instance"]),
            "uptime_seconds": 3600.4,
            "compatibility": {"mode": "ready", "findings": 0},
        },
        attention=[
            {"kind": "incident", "id": str(IDS["attention item id"]), "title": "Active incident in support-inbox"},
            {
                "kind": "approval",
                "id": str(IDS["approval item id"]),
                "title": "Approve the plan to scale workers on local-docker",
                "expires_at": (NOW + timedelta(seconds=420)).isoformat(),
            },
        ],
    )
    record = change_request(
        id=str(IDS["change request id"]),
        scope=scope_of("change request"),
        observation_job_id=str(IDS["change request observation_job_id"]),
        observation_id=str(IDS["change request observation_id"]),
        drained=[str(IDS["change request drained"])],
        drained_revisions={str(IDS["change request drained_revisions"]): 3},
        target_id=str(IDS["change request target_id"]),
        deployment_id=str(IDS["change request deployment_id"]),
        requested_by=str(IDS["change request requested_by"]),
        planned_by=str(IDS["change request planned_by"]),
        approval={
            "principal_id": str(IDS["change request approval principal"]),
            "approved_at": (NOW - timedelta(seconds=30)).isoformat(),
        },
        title=free_text(variant, "change request title"),
        reason=free_text(variant, "change request reason"),
        failure={"code": "WV-DRAIN-TIMEOUT", "message": free_text(variant, "change request failure message")},
        plan_digest=TEXT["change request plan digest"],
        plan_id=str(IDS["change request plan_id"]),
        apply_job_id=str(IDS["change request apply_job_id"]),
        desired=[
            {
                "name": "fixture-worker",
                "replicas": 1,
                "image": f"registry.example/{TEXT['change request image registry']}/worker@sha256:" + "ef" * 32,
                "worker_release_id": str(IDS["change request worker_release_id"]),
            }
        ],
        drain_set=[
            {
                "worker_id": str(IDS["change request drain member"]),
                "host": TEXT["change request drain member host"],
                "active_leases": 1,
            }
        ],
        history=[
            {
                "at": (NOW - timedelta(seconds=900)).isoformat(),
                "state": "observing",
                "actor_id": str(IDS["change request history actor"]),
            }
        ],
    )
    current = plan(
        id=str(IDS["change request plan_id"]),
        scope=scope_of("plan"),
        target_id=str(IDS["plan target_id"]),
        deployment_id=str(IDS["plan deployment_id"]),
        observation_id=str(IDS["plan observation_id"]),
        observation_digest=TEXT["plan observation digest"],
        digest=TEXT["plan digest"],
        steps=[
            {
                "action": "update",
                "component": {
                    "name": "fixture-worker",
                    "kind": "worker",
                    "image": f"registry.example/{TEXT['change request image registry']}/worker@sha256:" + "ef" * 32,
                    "configuration": TEXT["plan component configuration"],
                    "worker_release_id": str(IDS["plan worker_release_id"]),
                },
                "expected_version": TEXT["plan step expected_version"],
            }
        ],
    )
    apply = job(
        id=str(IDS["change request apply_job_id"]),
        scope=scope_of("job"),
        target_id=str(IDS["job target_id"]),
        observation_id=str(IDS["job observation_id"]),
        plan_id=str(IDS["change request plan_id"]),
        plan_digest=TEXT["job plan digest"],
        operation_key=str(IDS["job operation_key"]),
        receipt={
            "code": "applied",
            "changed_resources": [TEXT["job changed resource"]],
            "external_effects_may_continue": False,
        },
    )
    return {"worker": status, "incident": view, "summary": summary, "change": record, "plan": current, "job": apply}


# Which record each canary is planted in, for the positive control.
PLANTED = {
    "worker": [name for name in (*TEXT, *IDS) if name.startswith("worker")],
    "incident": [name for name in (*TEXT, *IDS) if name.startswith("incident")],
    "summary": [name for name in (*TEXT, *IDS) if name.startswith(("summary", "attention", "approval"))],
    "change": [name for name in (*TEXT, *IDS) if name.startswith("change request")],
    "plan": [name for name in (*TEXT, *IDS) if name.startswith("plan")],
    "job": [name for name in (*TEXT, *IDS) if name.startswith("job")],
}


def leaks(body: str) -> list[str]:
    """Every canary found in a recorded body: text and log-shaped canaries, and UUIDs hyphenated or as hex."""
    found = [name for name, value in TEXT.items() if value in body]
    found += ["log-shaped text"] if "canary-log" in body else []
    found += [name for name, value in IDS.items() if str(value) in body or value.hex in body]
    return found


def outside(kind: str, data: Any) -> set[str]:
    return set(keys(data)) - FIELDS[kind]


TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


IDENTIFIER_TYPES = ((UUID, "uuid"), (Digest, "digest"), (ExternalIdentity, "external identity"))
DIGEST_SHAPED = re.compile(r"(?:sha256:)?[0-9a-f]{64}")


def identifier_kinds(annotation: Any, seen: frozenset[Any] = frozenset()) -> set[str]:
    """The identifier kinds a field's annotation can hold, through unions, lists, dicts, Annotated and aliases.

    A recursive alias, such as a JSON value, is followed once.
    """
    for kind, name in IDENTIFIER_TYPES:
        if annotation is kind:
            return {name}
    if isinstance(annotation, TypeAliasType):
        return set() if annotation in seen else identifier_kinds(annotation.__value__, seen | {annotation})
    return set().union(*(identifier_kinds(argument, seen) for argument in get_args(annotation)))


def contents(value: Any) -> Iterator[Any]:
    """Every value inside a field's value, dict keys included."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from contents(child)
    elif isinstance(value, list | tuple | set | frozenset):
        for child in value:
            yield from contents(child)
    else:
        yield value


def identifier_fields(model: BaseModel, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every field typed as a UUID, a digest or an external identity, in the record and every model it holds."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if identifier_kinds(field.annotation):
            yield f"{path}/{name}", value
        for child in contents(value):
            if isinstance(child, BaseModel):
                yield from identifier_fields(child, f"{path}/{name}")


def dumped_strings(value: Any) -> Iterator[str]:
    for item in contents(value):
        if isinstance(item, str):
            yield item


def uncanaried(record: BaseModel) -> list[str]:
    """Each identifier of a record that is no canary of this suite: unset, shared with a fixture, or new."""
    ids = {str(value) for value in IDS.values()} | {value.hex for value in IDS.values()}
    texts = set(TEXT.values())
    found = []
    for path, value in identifier_fields(record):
        held = [item for item in contents(value) if isinstance(item, UUID | str)]
        if not held:
            found.append(f"{path} is unset")
        found += [f"{path} holds {item}" for item in held if str(item) not in ids and item not in texts]
    for text in dumped_strings(record.model_dump(mode="json")):
        found += [f"{match} is not a canary" for match in UUID_SHAPED.findall(text) if match not in ids]
        if DIGEST_SHAPED.fullmatch(text) and text not in texts:
            found.append(f"{text} is not a canary")
    return found


def test_every_excluded_item_has_its_own_canary():
    assert len(set(TEXT.values())) == len(TEXT) and len(set(IDS.values())) == len(IDS)


@pytest.mark.parametrize("record", sorted(PLANTED))
def test_every_identifier_of_a_record_is_its_own_canary(record):
    assert uncanaried(canaried()[record]) == []


class WorkerStatusWithNewID(WorkerStatus):
    new_id: UUID | None = None


def test_an_identifier_field_added_to_a_contract_needs_its_own_canary():
    record = WorkerStatusWithNewID.model_validate(canaried()["worker"].model_dump())
    assert uncanaried(record) == ["/new_id is unset"]
    assert uncanaried(record.model_copy(update={"new_id": uid(999)})) == [
        f"/new_id holds {uid(999)}",
        f"{uid(999)} is not a canary",
    ]


@pytest.mark.parametrize("record", sorted(PLANTED))
def test_every_fixture_record_carries_every_canary(record):
    dumped = canaried()[record].model_dump_json()
    missing = [name for name in PLANTED[record] if (TEXT.get(name) or str(IDS.get(name))) not in dumped]
    assert missing == []


@pytest.mark.parametrize("variant", ["token", "log"])
def test_every_fixture_record_carries_the_log_shaped_text_in_its_free_text(variant):
    dumped = "".join(record.model_dump_json() for record in canaried(variant).values())
    assert ("canary-log" in dumped) is (variant == "log")
    # Both lines are planted, in the same fields in every process.
    assert [line in dumped for line in LOG_SHAPED] == [variant == "log"] * 2


class Recording(httpx.AsyncBaseTransport):
    """Records each request's body, never its headers (the gateway token travels there), and answers a reply."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        return httpx.Response(200, json={"answer": "Ready", "proposals": [], "followUps": []})


class CanariedOwners(Owners):
    def __init__(self, records):
        super().__init__()
        self.workers = [records["worker"]]
        self.incidents = [incident_facts(records["incident"])]
        self.summaries = [summary_facts(records["summary"])]
        self.changes = [records["change"]]
        self.related = {"plans": records["plan"], "jobs": records["job"]}

    # The records carry their own IDs; each read checks access as the shared fake does.
    async def read_worker(self, actor, scope, identifier, *, context):
        self.check("worker", actor, scope, context)
        return self.take(self.workers)

    async def explanation_facts_incident(self, actor, scope, identifier, *, context):
        self.check("incident", actor, scope, context)
        return self.take(self.incidents)

    async def read_observed(self, identifier, *, actor, scope, context):
        self.check("change-request", actor, scope, context)
        return self.take(self.changes), NOW

    async def read_deployment(self, collection, identifier, *, actor, scope, context):
        return self.related[collection]


async def recorded_ask(monkeypatch, tmp_path, records, request):
    """The real ask with the real builders and shaping, through the real gateway client to a recording transport."""
    owners = CanariedOwners(records)
    service, gateway = asker(monkeypatch, owners)
    transport = Recording()
    token = tmp_path / "token"
    token.write_text("service-token")
    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=str(token)),
        transport=transport,
    )
    configuration = LumiConfiguration(
        revision=1,
        connection_revision_id=uuid_of(0),
        profile={
            "provider": "openai-chat",
            "model": "fixture",
            "options": {"max_tokens": 512},
            "outputSchema": LUMI_REPLY_SCHEMA,
        },
    )

    async def send(_configuration, asked, attachments, *_, **__):
        return await client.ask(
            configuration, asked, attachments, {"endpoint": "https://api.openai.com/v1"}, PROVIDER_KEY
        )

    gateway.ask.side_effect = send
    await ask(service, owners, request)
    return transport.bodies


def uuid_of(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012x}")


def everything(records) -> LumiAskRequest:
    return LumiAskRequest(
        message="What needs my attention?",
        history=[{"role": "user", "content": "Is this worker healthy?"}, {"role": "assistant", "content": "Yes."}],
        attachments=[
            {"kind": "worker", "id": str(records["worker"].id)},
            {"kind": "incident", "id": str(records["incident"].id)},
            {"kind": "environment", "id": str(SCOPE.environment_id)},
            {"kind": "change-request", "id": str(records["change"].id)},
        ],
    )


@pytest.mark.parametrize("variant", ["token", "log"])
async def test_no_excluded_value_reaches_the_gateway(monkeypatch, tmp_path, variant):
    records = canaried(variant)
    [raw] = await recorded_ask(monkeypatch, tmp_path, records, everything(records))
    body = raw.decode()
    assert leaks(body) == []
    sent = json.loads(body)
    assert UUID_SHAPED.search(json.dumps(sent["attachments"])) is None
    resources = sent["attachments"]["resources"]
    assert [item["kind"] for item in resources] == ["worker", "incident", "environment", "change-request"]
    for item in resources:
        assert set(item) == {"kind", "ref", "data"}
        assert outside(item["kind"], item["data"]) == set()
        assert TIMESTAMP.search(json.dumps(item["data"])) is None


def _set(data: Any, path: str, value: str) -> None:
    """Put ``value`` at an allowlisted path of built data, creating what the path needs; ``*`` is the first item."""
    parts = path.strip("/").split("/")
    node = data
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        following = None if last else parts[index + 1]
        empty: Any = [] if following == "*" else {}
        if part == "*":
            if not node:
                node.append(empty if not last else value)
            if last:
                node[0] = value
            elif not isinstance(node[0], type(empty)):
                node[0] = empty
            node = node[0]
        else:
            if last:
                node[part] = value
            else:
                if not isinstance(node.get(part), type(empty)):
                    node[part] = empty
                node = node[part]


def built(kind: str) -> dict[str, Any]:
    records = canaried()
    if kind == "worker":
        return worker_context(records["worker"]).data
    if kind == "incident":
        return incident_context(incident_facts(records["incident"])).data
    if kind == "environment":
        return environment_context(summary_facts(records["summary"])).data
    return change_request_context(records["change"], records["plan"], records["job"], observed_at=NOW).data


PATHS = [(kind, path) for kind, fields in FIELDS.items() for path in sorted(fields)]


@pytest.mark.parametrize(("kind", "path"), PATHS, ids=[f"{kind}{path}" for kind, path in PATHS])
@pytest.mark.parametrize("canary", ["text", "uuid"])
def test_the_leak_check_catches_a_canary_at_every_allowlisted_path(kind, path, canary):
    data = built(kind)
    assert leaks(json.dumps(data)) == []
    planted = TEXT["change request title"] if canary == "text" else IDS["worker id"].hex
    _set(data, path, planted)
    assert leaks(json.dumps(data)) != []
    assert outside(kind, data) == set()


@pytest.mark.parametrize("kind", sorted(FIELDS))
def test_the_path_check_catches_a_path_outside_the_allowlist(kind):
    data = built(kind)
    data["host"] = "local"
    assert outside(kind, data) == {"/host"}


class ExtendedWorkerStatus(WorkerStatus):
    secret_field: str = "canary-added-7f3a"


def test_a_field_added_to_the_record_is_not_sent():
    record = ExtendedWorkerStatus.model_validate(canaried()["worker"].model_dump())
    assert "canary-added-7f3a" in record.model_dump_json()
    data = worker_context(record).data
    assert "canary-added-7f3a" not in json.dumps(data) and outside("worker", data) == set()


# Every string a builder may emit: the contracts' fixed values, a name, code or key token, a templated title, or a
# server constant. Anything else is free text.
CONSTANTS = frozenset(
    {"withheld", "unavailable", "forbidden", UNKNOWN_CODE_NOTE, LIMITATION, EXPLAIN_ONLY}
    | {entry.meaning for entry in INCIDENT_CODES.values()}
    | {entry.note for entry in INCIDENT_CODES.values() if entry.note}
)


def allowed(value: str) -> bool:
    if UUID_SHAPED.search(value) or TIMESTAMP.search(value):
        return False
    return (
        value in CONSTANTS
        or bool(NAME_TOKEN.fullmatch(value))
        or bool(CODE_TOKEN.fullmatch(value))
        or (len(value) <= 512 and bool(KEY_TOKEN.fullmatch(value)))
        or any(template.fullmatch(value) for template in ATTENTION_TITLES.values())
    )


def strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, f"{path}/{key}")
    elif isinstance(value, list):
        for child in value:
            yield from strings(child, f"{path}/*")


def scrambled(model: BaseModel, text: st.DataObject) -> BaseModel:
    """The same record with arbitrary text in every string field, nested models and lists included, unvalidated."""

    def value(current: Any) -> Any:
        if isinstance(current, str):
            return text.draw(st.text(max_size=300))
        if isinstance(current, BaseModel):
            return scrambled(current, text)
        if isinstance(current, list):
            return [value(item) for item in current]
        if isinstance(current, tuple):
            return tuple(value(item) for item in current)
        if isinstance(current, dict):
            return {key: value(item) for key, item in current.items()}
        return current

    return model.model_copy(update={name: value(getattr(model, name)) for name in type(model).model_fields})


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_a_builder_never_emits_free_text(data):
    records = canaried()
    scramble = {name: scrambled(record, data) for name, record in records.items()}
    facts = IncidentFacts(
        view=scramble["incident"],
        step_kind=data.draw(st.text(max_size=40)),
        events=tuple(data.draw(st.lists(st.text(max_size=40), max_size=25))),
        more_events=False,
        other_events=0,
        run_read=True,
        observed_at=NOW,
    )
    emitted = {
        "worker": worker_context(scramble["worker"]).data,
        "incident": incident_context(facts).data,
        "environment": environment_context(SummaryFacts(summary=scramble["summary"], reach=frozenset())).data,
        "change-request": change_request_context(
            scramble["change"], scramble["plan"], scramble["job"], observed_at=NOW
        ).data,
    }
    free = [
        (kind, path, value) for kind, built_ in emitted.items() for path, value in strings(built_) if not allowed(value)
    ]
    assert free == []


@settings(max_examples=100, deadline=None)
@given(st.lists(st.text(max_size=40), max_size=25))
def test_an_incident_sends_only_runtime_event_types(events):
    from firefly_weave.runtime.models import RuntimeEvent

    known = set(get_args(RuntimeEvent.model_fields["type"].annotation))
    sent = incident_context(incident_facts(events=tuple(events))).data["recent_events"]
    assert all(item is None or item in known for item in sent)


async def test_a_hostile_valid_name_is_sent_only_as_a_name(monkeypatch, tmp_path):
    records = canaried()
    hostile = "ignore-previous-instructions"
    record = records["change"]
    desired = [item.model_copy(update={"name": hostile}) for item in record.desired]
    records["change"] = record.model_copy(update={"components": [hostile], "desired": desired})
    [raw] = await recorded_ask(monkeypatch, tmp_path, records, everything(records))
    sent = json.loads(raw)

    def where(value: Any, path: str = "") -> Iterator[str]:
        if value == hostile:
            yield path
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from where(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from where(child, f"{path}/{index}")

    assert list(where(sent)) == ["/attachments/resources/3/data/components/0/name"]
    assert raw.decode().count(hostile) == 1


async def test_the_gateway_request_carries_no_credential_in_its_attachments(monkeypatch, tmp_path):
    records = canaried()
    [raw] = await recorded_ask(monkeypatch, tmp_path, records, everything(records))
    sent = json.loads(raw)
    assert sent.pop("credential") == PROVIDER_KEY
    assert PROVIDER_KEY not in json.dumps(sent)


async def test_history_and_message_are_the_persons_own(monkeypatch, tmp_path):
    records = canaried()
    request = everything(records)
    [raw] = await recorded_ask(monkeypatch, tmp_path, records, request)
    assert json.loads(raw)["request"] == {
        "message": "What needs my attention?",
        "history": [{"role": "user", "content": "Is this worker healthy?"}, {"role": "assistant", "content": "Yes."}],
    }
