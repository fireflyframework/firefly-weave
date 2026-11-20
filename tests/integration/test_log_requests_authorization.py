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

"""Container log requests on real PostgreSQL: who may ask and read, when the target's runner may answer, the rate per
target, what the server keeps of a report, and how long it keeps it."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.deployments import (
    LogLine,
    LogReport,
    LogRequestCreate,
    MemberLog,
    ObserveRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.leases import DeploymentLeases
from firefly_weave.deployments.log_requests import LogRequestService
from firefly_weave.deployments.service import DeploymentService

pytestmark = pytest.mark.integration
ALL = ["observe", "scale_workers", "read_logs"]
# Secret-shaped values are built from parts at run time, so the repository holds no secret-shaped text.
TOKEN = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "c2lnbmF0dXJlLXBhcnQ"


@pytest.fixture
async def logs(services, access_db, provisioned):
    admin, scopes = provisioned
    scope = scopes[0]
    people = {}
    granted = {
        "runner": (("deployment_planner", "deployment_runner"), ()),
        "reader": (("deployment_log_reader", "deployment_reader"), ()),
        "logs_only": (("deployment_log_reader",), ()),
        "deployment_only": (("deployment_reader",), ()),
    }
    for name, (roles, _) in granted.items():
        principal = await access_db[2].create_principal(admin, "application")
        for role in roles:
            await access_db[2].grant(admin, principal, Grant(role=role, scope=scope))
        people[name] = await access_db[2].load_principal(principal)
    graph = services(access_db[0])
    service, requests = graph.resolve(DeploymentService), graph.resolve(LogRequestService)

    def authority(name):
        return dict(actor=people[name], scope=scope, context=AuditContext())

    async def target(*, capabilities=ALL, registered=ALL):
        created = await service.create_target(
            TargetRequest(
                name="local-" + uuid4().hex[:8],
                adapter="docker-compose",
                external_identity="owned-" + uuid4().hex[:8],
                boundary="weave",
                runner_principal_id=people["runner"].id,
                capabilities=capabilities,
            ),
            uuid4().hex,
            **authority("runner"),
        )
        runner = await service.register_runner(
            RunnerRegistration(target_id=created.id, adapter="docker-compose", capabilities=registered),
            **authority("runner"),
        )
        return SimpleNamespace(target=created, runner=runner)

    return SimpleNamespace(
        service=service,
        requests=requests,
        authority=authority,
        people=people,
        target=target,
        access=access_db,
        admin=admin,
        scope=scope,
    )


def asked(target, **values) -> LogRequestCreate:
    return LogRequestCreate.model_validate(
        {"target_id": target.id, "component": "fixture-worker", "since_seconds": 900, "tail_lines": 200, **values}
    )


async def execute(logs, statement: str, **values) -> None:
    async with logs.access[1].begin() as session:
        await session.execute(text(statement), values)


async def refused(call) -> CatalogError:
    with pytest.raises(CatalogError) as error:
        await call
    return error.value


async def test_a_person_without_deployment_log_read_or_deployment_read_on_the_target_is_refused(logs):
    first = await logs.target()
    for name in ("logs_only", "deployment_only"):
        with pytest.raises(AccessDenied):
            await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority(name))
    created = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    assert created.state == "pending" and created.requested_members == []
    for name in ("logs_only", "deployment_only"):
        with pytest.raises(AccessDenied):
            await logs.requests.read(created.id, **logs.authority(name))
        # Refused before the lookup, so a missing request says nothing to them either.
        with pytest.raises(AccessDenied):
            await logs.requests.read(uuid4(), **logs.authority(name))
    assert (await refused(logs.requests.read(uuid4(), **logs.authority("reader")))).status == 404
    assert (await logs.requests.read(created.id, **logs.authority("reader"))).id == created.id


async def test_a_grant_for_one_target_reads_only_that_targets_logs(logs):
    first, second = await logs.target(), await logs.target()
    principal = await logs.access[2].create_principal(logs.admin, "application")
    for role in ("deployment_log_reader", "deployment_reader"):
        await logs.access[2].grant(
            logs.admin, principal, Grant(role=role, scope=logs.scope, resources=(str(first.target.id),))
        )
    logs.people["restricted"] = await logs.access[2].load_principal(principal)
    mine = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("restricted"))
    with pytest.raises(AccessDenied):
        await logs.requests.create(asked(second.target), uuid4().hex, **logs.authority("restricted"))
    theirs = await logs.requests.create(asked(second.target), uuid4().hex, **logs.authority("reader"))
    assert (await logs.requests.read(mine.id, **logs.authority("restricted"))).target_id == first.target.id
    with pytest.raises(AccessDenied):
        await logs.requests.read(theirs.id, **logs.authority("restricted"))


async def test_a_target_or_runner_without_read_logs_cant_fetch_logs(logs):
    for kind in (
        await logs.target(capabilities=["observe", "scale_workers"], registered=["observe"]),
        await logs.target(registered=["observe", "scale_workers"]),
    ):
        error = await refused(logs.requests.create(asked(kind.target), uuid4().hex, **logs.authority("reader")))
        assert (error.status, error.code) == (422, "WV-DEPLOYMENT-UNSUPPORTED")


async def test_a_runner_out_of_touch_for_90_seconds_is_refused(logs):
    offline = await logs.target()
    await execute(
        logs,
        "UPDATE deployment_runners SET payload=jsonb_set(jsonb_set(payload,'{expires_at}',"
        "to_jsonb(now()-interval '30 seconds')),'{last_seen}',to_jsonb(now()-interval '120 seconds')) "
        "WHERE target_id=:target",
        target=offline.target.id,
    )
    error = await refused(logs.requests.create(asked(offline.target), uuid4().hex, **logs.authority("reader")))
    assert (error.status, error.code) == (409, "WV-RUNNER-OFFLINE")
    assert error.result is not None and error.result["last_seen"]
    # Claiming, as every runner call, counts as being in touch again.
    await logs.requests.claim(RunnerClaimRequest(runner_id=offline.runner.id), **logs.authority("runner"))
    assert (await logs.requests.create(asked(offline.target), uuid4().hex, **logs.authority("reader"))).state == (
        "pending"
    )


async def test_the_eleventh_request_in_a_minute_for_one_target_is_refused(logs):
    busy, quiet = await logs.target(), await logs.target()
    for _ in range(10):
        await logs.requests.create(asked(busy.target), uuid4().hex, **logs.authority("reader"))
    error = await refused(logs.requests.create(asked(busy.target), uuid4().hex, **logs.authority("reader")))
    assert (error.status, error.code) == (429, "WV-LOG-RATE")
    assert error.retry_after is not None and 1 <= error.retry_after <= 60
    # Another target keeps its own count; a minute later the first takes requests again.
    await logs.requests.create(asked(quiet.target), uuid4().hex, **logs.authority("reader"))
    await execute(
        logs,
        "UPDATE deployment_log_requests SET created_at=created_at-interval '61 seconds' WHERE target_id=:target",
        target=busy.target.id,
    )
    await logs.requests.create(asked(busy.target), uuid4().hex, **logs.authority("reader"))


async def test_the_same_key_answers_the_same_request_once(logs):
    first = await logs.target()
    key = uuid4().hex
    created = await logs.requests.create(asked(first.target), key, **logs.authority("reader"))
    again = await logs.requests.create(asked(first.target), key, **logs.authority("reader"))
    assert again.id == created.id
    error = await refused(logs.requests.create(asked(first.target, tail_lines=10), key, **logs.authority("reader")))
    assert error.code == "WV-IDEMPOTENCY-CONFLICT"


def report(claim, lines, **values) -> LogReport:
    return LogReport.model_validate(
        {"proof": claim.proof, "members": [MemberLog(member="weave-worker-1", lines=lines)], **values}
    )


async def test_the_runner_reports_and_the_person_reads_what_the_server_redacted_again(logs):
    first = await logs.target()
    created = await logs.requests.create(
        asked(first.target, members=["weave-worker-1"]), uuid4().hex, **logs.authority("reader")
    )
    claim = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert claim is not None and claim.proof.request_id == created.id
    assert (claim.component, claim.members, claim.since_seconds, claim.tail_lines) == (
        "fixture-worker",
        ["weave-worker-1"],
        900,
        200,
    )
    assert claim.target.id == first.target.id and "read_logs" in claim.target.capabilities
    # Nothing else waits: a second claim finds nothing while the first is current.
    assert await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner")) is None
    lines = [
        LogLine(at=created.created_at, stream="stdout", text="Worker weave-worker-1 ready: 4 slots"),
        LogLine(at=created.created_at, stream="stderr", text=f"WARN relay answered 550; Authorization: Bearer {TOKEN}"),
    ]
    accepted = await logs.requests.report(report(claim, lines), **logs.authority("runner"))
    assert (accepted.request_id, accepted.state) == (created.id, "ready")
    view = await logs.requests.read(created.id, **logs.authority("reader"))
    assert view.state == "ready" and view.read_at is not None
    texts = [line.text for line in view.members[0].lines]
    assert texts[0] == "Worker weave-worker-1 ready: 4 slots"
    assert TOKEN not in texts[1] and "[redacted]" in texts[1]
    async with logs.access[1]() as session:
        stored = await session.scalar(text("SELECT members::text FROM deployment_log_snapshots"))
    assert TOKEN not in stored
    # The same claim reported again keeps its first report.
    again = await logs.requests.report(report(claim, lines[:1]), **logs.authority("runner"))
    assert again.state == "ready"
    assert len((await logs.requests.read(created.id, **logs.authority("reader"))).members[0].lines) == 2


async def test_a_report_with_another_token_after_its_claim_or_for_other_members_is_refused(logs):
    first = await logs.target()
    created = await logs.requests.create(
        asked(first.target, members=["weave-worker-1"]), uuid4().hex, **logs.authority("reader")
    )
    claim = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert claim is not None
    forged = claim.model_copy(update={"proof": claim.proof.model_copy(update={"token": uuid4()})})
    error = await refused(logs.requests.report(report(forged, []), **logs.authority("runner")))
    assert (error.status, error.code) == (409, "WV-RUNNER-FENCE")
    other = LogReport.model_validate({"proof": claim.proof, "members": [MemberLog(member="weave-worker-2")]})
    assert (await refused(logs.requests.report(other, **logs.authority("runner")))).status == 422
    await execute(
        logs,
        "UPDATE deployment_log_requests SET claim_expires_at=now()-interval '1 second' WHERE id=:id",
        id=created.id,
    )
    error = await refused(logs.requests.report(report(claim, []), **logs.authority("runner")))
    assert (error.status, error.code) == (409, "WV-RUNNER-FENCE")
    # A claim that lapsed may be claimed again, with a new token.
    again = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert again is not None and again.proof.request_id == created.id and again.proof.token != claim.proof.token


async def test_a_runner_that_never_answers_leaves_the_request_failed_and_a_snapshot_expires_after_an_hour(logs):
    first = await logs.target()
    late = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    await execute(
        logs,
        "UPDATE deployment_log_requests SET created_at=created_at-interval '121 seconds' WHERE id=:id",
        id=late.id,
    )
    view = await logs.requests.read(late.id, **logs.authority("reader"))
    assert (view.state, view.failure) == ("failed", "no_answer")
    assert await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner")) is None
    ready = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    claim = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert claim is not None
    line = LogLine(at=ready.created_at, stream="stdout", text="ready")
    await logs.requests.report(report(claim, [line]), **logs.authority("runner"))
    await execute(
        logs,
        "UPDATE deployment_log_requests SET created_at=now()-interval '61 minutes',"
        "expires_at=now()-interval '1 minute' WHERE id=:id",
        id=ready.id,
    )
    view = await logs.requests.read(ready.id, **logs.authority("reader"))
    assert (view.state, view.members) == ("expired", [])


async def test_a_log_request_never_waits_behind_a_job(logs):
    first = await logs.target()
    job = await logs.service.observe(ObserveRequest(target_id=first.target.id), uuid4().hex, **logs.authority("runner"))
    created = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    claim = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert claim is not None and claim.proof.request_id == created.id
    # The job is still there for the runner's own claim, untouched by the log request.
    lease = await DeploymentLeases(logs.service).claim(
        RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner")
    )
    assert lease is not None and lease.job.id == job.id


async def test_a_runner_claims_only_its_own_targets_requests(logs):
    first, second = await logs.target(), await logs.target()
    await logs.requests.create(asked(second.target), uuid4().hex, **logs.authority("reader"))
    assert await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner")) is None
    for name in ("reader", "logs_only"):
        with pytest.raises(AccessDenied):
            await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority(name))
    error = await refused(
        logs.requests.claim(RunnerClaimRequest(runner_id=uuid4()), **logs.authority("runner")),
    )
    assert error.status == 404


async def test_expiry_lasts_an_hour_from_the_request(logs):
    first = await logs.target()
    created = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    assert created.expires_at - created.created_at == timedelta(hours=1)


async def test_a_report_after_read_logs_was_removed_from_the_target_keeps_no_lines(logs):
    first = await logs.target()
    created = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
    claim = await logs.requests.claim(RunnerClaimRequest(runner_id=first.runner.id), **logs.authority("runner"))
    assert claim is not None
    # Between the claim and its report, a target manager takes read_logs away.
    await logs.service.update_target(
        first.target.id,
        TargetUpdate(runner_principal_id=first.target.runner_principal_id, capabilities=["observe", "scale_workers"]),
        first.target.revision,
        **logs.authority("runner"),
    )
    line = LogLine(at=created.created_at, stream="stdout", text="ready")
    error = await refused(logs.requests.report(report(claim, [line]), **logs.authority("runner")))
    assert (error.status, error.code) == (409, "WV-RUNNER-FENCE")
    assert (await logs.requests.read(created.id, **logs.authority("reader"))).members == []
    async with logs.access[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM deployment_log_snapshots")) == 0


async def test_fifteen_requests_at_once_for_one_target_create_ten(logs):
    first = await logs.target()

    async def ask() -> str:
        """Ask until the platform answers more than "busy": created, or refused for the rate."""
        while True:
            try:
                await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("reader"))
                return "created"
            except CatalogError as error:
                if error.code != "WV-OPERATION-CAPACITY":
                    return error.code

    answers = await asyncio.gather(*(ask() for _ in range(15)))
    assert sorted(answers) == ["WV-LOG-RATE"] * 5 + ["created"] * 10
    async with logs.access[1]() as session:
        stored = await session.scalar(
            text("SELECT count(*) FROM deployment_log_requests WHERE target_id=:target"), {"target": first.target.id}
        )
    assert stored == 10


async def test_a_token_whose_log_reader_grant_was_revoked_reads_nothing_not_even_a_missing_request(logs):
    first = await logs.target()
    principal = await logs.access[2].create_principal(logs.admin, "application")
    binding = await logs.access[2].grant(logs.admin, principal, Grant(role="deployment_log_reader", scope=logs.scope))
    await logs.access[2].grant(logs.admin, principal, Grant(role="deployment_reader", scope=logs.scope))
    logs.people["revoked"] = await logs.access[2].load_principal(principal)
    created = await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("revoked"))
    # The token still names the role; the stored grant is gone.
    await logs.access[2].revoke_grant(logs.admin, logs.scope, binding)
    for identifier in (created.id, uuid4()):
        with pytest.raises(AccessDenied):
            await logs.requests.read(identifier, **logs.authority("revoked"))
    with pytest.raises(AccessDenied):
        await logs.requests.create(asked(first.target), uuid4().hex, **logs.authority("revoked"))
