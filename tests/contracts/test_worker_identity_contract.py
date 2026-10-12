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

"""Worker identity and observed members are bounded, optional and byte-stable when absent."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.deployments import (
    DeploymentObservation,
    ObservationReport,
    ObservedMember,
    ObservedResource,
)
from firefly_weave.contracts.workers import InstanceRequest, WorkerIdentity, WorkerInstance, WorkerStatus


@pytest.mark.parametrize("host", ["-host", "host/path", "host\nother", "host\n", "a" * 254, "", "host name"])
def test_worker_host_is_bounded(host):
    with pytest.raises(ValidationError):
        WorkerIdentity(host=host, runtime="process")


def test_worker_host_accepts_names_and_pod_names():
    for host in ("a" * 253, "worker-7f9c5d", "weave-worker-5d8f7c9b6-x2kq4", "host.example.com", "0a1b2c3d4e5f"):
        assert WorkerIdentity(host=host, runtime="container").host == host


def test_identity_dump_leaves_out_unknown_values():
    identity = WorkerIdentity(runtime="process")
    assert identity.model_dump(mode="json") == {"runtime": "process", "platform_hint": "none"}
    assert identity.model_dump(mode="json") == identity.model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize("field", ["sdk_version", "language_version", "os", "arch"])
@pytest.mark.parametrize("value", ["a" * 65, "", "3.12\n", "-x", "x y", "x\x00"])
def test_reported_versions_are_bounded(field, value):
    with pytest.raises(ValidationError):
        WorkerIdentity(runtime="process", **{field: value})


@pytest.mark.parametrize("field", ["namespace", "node"])
def test_kubernetes_names_are_bounded(field):
    assert getattr(WorkerIdentity(runtime="container", **{field: "a" * 253}), field) == "a" * 253
    for value in ("a" * 254, "ns/other", "-ns", ""):
        with pytest.raises(ValidationError):
            WorkerIdentity(runtime="container", **{field: value})


def test_runtime_and_platform_hint_are_closed_vocabularies():
    for runtime in ("embedded", "container", "process"):
        WorkerIdentity(runtime=runtime)
    for hint in ("kubernetes", "docker", "none"):
        WorkerIdentity(runtime="container", platform_hint=hint)
    with pytest.raises(ValidationError):
        WorkerIdentity(runtime="vm")
    with pytest.raises(ValidationError):
        WorkerIdentity(runtime="process", platform_hint="nomad")
    with pytest.raises(ValidationError):
        WorkerIdentity()


@pytest.mark.parametrize(
    "labels",
    [
        {str(index): "x" for index in range(9)},
        {"": "x"},
        {" team": "x"},
        {"k" * 65: "x"},
        {"team": "x" * 257},
        {"team": "line\nbreak"},
        {"team": "tab\there"},
        {"team": "bell\x07"},
        {"team": "c1\x85"},
        {"api_token": "x"},
        {"DB-Password": "x"},
        {"client.secret": "x"},
        {"Authorization": "x"},
        {"team=a": "x"},
        {"team,a": "x"},
    ],
)
def test_labels_are_bounded_and_never_name_credentials(labels):
    with pytest.raises(ValidationError):
        WorkerIdentity(runtime="process", labels=labels)


def test_labels_accept_eight_bounded_pairs():
    labels = {f"app.example/key-{index}": "v" * 256 for index in range(8)}
    labels["app.example/key-0"] = ""
    identity = WorkerIdentity(runtime="process", labels=labels)
    assert identity.labels == labels
    assert "labels" not in WorkerIdentity(runtime="process").model_dump(mode="json")


def test_identity_is_limited_to_what_its_database_column_stores():
    def identity(value):
        return WorkerIdentity(
            host="h" * 253,
            runtime="container",
            sdk_version="s" * 64,
            language_version="l" * 64,
            os="o" * 64,
            arch="a" * 64,
            platform_hint="kubernetes",
            namespace="n" * 253,
            node="d" * 253,
            labels={f"key-{index}": value for index in range(8)},
        )

    with pytest.raises(ValidationError, match="4 KiB"):
        identity("é" * 256)
    small = identity("v" * 100)
    text = json.dumps(small.model_dump(mode="json"), ensure_ascii=False, separators=(", ", ": "))
    assert len(text.encode()) <= 4096


def test_unknown_identity_and_registration_fields_are_refused():
    base = {"release_id": str(uuid4()), "task_types": ["echo@1.0.0"], "capacity": 1}
    placement = {"target_id": str(uuid4()), "target_name": "t", "deployment_id": str(uuid4()), "component": "w"}
    with pytest.raises(ValidationError):
        InstanceRequest.model_validate_json(json.dumps({**base, "placement": placement}))
    with pytest.raises(ValidationError):
        InstanceRequest.model_validate_json(
            json.dumps({**base, "identity": {"runtime": "process", "placement": placement}})
        )
    with pytest.raises(ValidationError):
        InstanceRequest.model_validate_json(json.dumps({**base, "registered_at": "2026-10-01T00:00:00Z"}))


def test_request_without_identity_keeps_its_bytes():
    request = InstanceRequest(release_id=uuid4(), task_types=["echo@1.0.0"], capacity=1)
    assert set(request.model_dump(mode="json")) == {"release_id", "task_types", "capacity"}
    with_identity = request.model_copy(update={"identity": WorkerIdentity(host="worker-1", runtime="process")})
    assert with_identity.model_dump(mode="json")["identity"] == {
        "host": "worker-1",
        "runtime": "process",
        "platform_hint": "none",
    }
    parsed = InstanceRequest.model_validate_json(with_identity.model_dump_json())
    assert parsed == with_identity


def test_old_worker_instance_and_status_answers_still_deserialize():
    instance = {
        "release_id": str(uuid4()),
        "task_types": ["echo@1.0.0"],
        "capacity": 2,
        "id": str(uuid4()),
        "principal_id": str(uuid4()),
        "revoked": False,
    }
    parsed = WorkerInstance.model_validate_json(json.dumps(instance))
    assert parsed.identity is None and json.loads(parsed.model_dump_json()) == instance
    now = datetime.now(UTC).isoformat()
    status = {
        **instance,
        "revision": 1,
        "draining": False,
        "presence": "unknown",
        "last_seen_at": None,
        "presence_expires_at": None,
        "presence_ttl_seconds": 60,
        "observed_at": now,
        "active_leases": 0,
        "available_capacity": None,
    }
    old = WorkerStatus.model_validate_json(json.dumps(status))
    assert old.identity is None and old.registered_at is None
    assert set(json.loads(old.model_dump_json())) == set(status)


def test_status_reports_identity_and_registration_time_when_known():
    registered = datetime(2026, 10, 1, tzinfo=UTC)
    status = WorkerStatus(
        release_id=uuid4(),
        task_types=["echo@1.0.0"],
        capacity=1,
        id=uuid4(),
        principal_id=uuid4(),
        revision=1,
        draining=False,
        presence="unknown",
        last_seen_at=None,
        presence_expires_at=None,
        observed_at=registered,
        active_leases=0,
        available_capacity=None,
        identity=WorkerIdentity(host="worker-1", runtime="process"),
        registered_at=registered,
    )
    dumped = status.model_dump(mode="json")
    assert dumped["identity"]["host"] == "worker-1"
    assert dumped["registered_at"] == "2026-10-01T00:00:00Z"
    assert WorkerStatus.model_validate_json(status.model_dump_json()) == status


def resource(**changes):
    values = dict(
        name="worker",
        external_identity="compose/test/worker",
        kind="worker",
        image=None,
        replicas=0,
        ready_replicas=0,
        version="stopped",
        ownership="managed",
        state="stopped",
    )
    return ObservedResource(**{**values, **changes})


def member(host="0a1b2c3d4e5f", **changes):
    values = dict(
        host=host,
        ordinal=1,
        state="running",
        ready=True,
        started_at=datetime(2026, 10, 1, tzinfo=UTC),
        restarts=0,
        node=None,
    )
    return ObservedMember(**{**values, **changes})


def test_observation_without_members_preserves_digest():
    report = ObservationReport(resources=[resource()], complete=True, settled=True)
    assert "members" not in report.model_dump(mode="json")["resources"][0]
    assert report.model_dump(mode="json") == report.model_copy(
        update={"resources": [resource().model_copy(update={"members": []})]}
    ).model_dump(mode="json")
    legacy = {
        "settled": True,
        "complete": True,
        "resources": [
            {
                "name": "worker",
                "external_identity": "compose/test/worker",
                "kind": "worker",
                "image": None,
                "replicas": 0,
                "ready_replicas": 0,
                "version": "stopped",
                "ownership": "managed",
                "state": "stopped",
            }
        ],
    }
    assert report.model_dump(mode="json") == legacy
    stored = {
        "id": str(uuid4()),
        "target_id": str(uuid4()),
        "target_revision": 1,
        "observed_at": "2026-10-01T00:00:00Z",
        "expires_at": "2026-10-01T00:15:00Z",
        "digest": "0" * 64,
    }
    # The server stores a runner report as it does in deployments/leases.py.
    observation = DeploymentObservation.model_validate_json(json.dumps({**report.model_dump(mode="json"), **stored}))
    legacy_observation = DeploymentObservation.model_validate_json(json.dumps({**legacy, **stored}))
    assert canonical_digest(observation.model_dump(mode="json", exclude={"digest"})) == canonical_digest(
        legacy_observation.model_dump(mode="json", exclude={"digest"})
    )


def test_members_are_reported_and_bounded():
    observed = resource(replicas=2, ready_replicas=1, members=[member(), member("1b2c3d4e5f6a", ordinal=2)])
    dumped = observed.model_dump(mode="json")["members"]
    assert dumped[0] == {
        "host": "0a1b2c3d4e5f",
        "ordinal": 1,
        "state": "running",
        "ready": True,
        "started_at": "2026-10-01T00:00:00Z",
        "restarts": 0,
        "node": None,
    }
    assert ObservedResource.model_validate_json(observed.model_dump_json()) == observed
    resource(members=[member(f"host-{index}") for index in range(100)])
    with pytest.raises(ValidationError):
        resource(members=[member(f"host-{index}") for index in range(101)])


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "host/path"},
        {"host": "-host"},
        {"host": "a" * 254},
        {"ordinal": -1},
        {"state": ""},
        {"state": "Running now"},
        {"state": "s" * 33},
        {"restarts": -1},
        {"node": "node/other"},
        {"started_at": datetime(2026, 10, 1)},
    ],
)
def test_member_fields_are_bounded(changes):
    with pytest.raises(ValidationError):
        member(**changes)


def test_members_carry_no_worker_authority_fields():
    payload = member().model_dump(mode="json")
    for forged in ("worker_id", "placement", "principal_id", "target_id"):
        with pytest.raises(ValidationError):
            ObservedMember.model_validate_json(json.dumps({**payload, forged: str(uuid4())}))


def test_identity_and_member_contracts_are_exported():
    from firefly_weave.contracts.schema_export import export_schemas

    schemas = export_schemas()
    assert "worker-identity" in schemas and "operations-observed-member" in schemas
    request = schemas["worker-instance-request"]
    assert "identity" in request["properties"] and "identity" not in request.get("required", [])
