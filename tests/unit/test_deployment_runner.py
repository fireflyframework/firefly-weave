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

"""Runner fencing and local authority apply even to valid API payloads."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    ComponentSpec,
    Deployment,
    DeploymentJob,
    DeploymentLease,
    DeploymentLeaseProof,
    DeploymentObservation,
    DeploymentPlan,
    DeploymentTarget,
    ObservationReport,
    ObservedResource,
    PlanStep,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import OperationsRunner, RunnerPolicyError, validate_lease


def fixture():
    now = datetime.now(UTC)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    target = DeploymentTarget(
        id=uuid4(),
        scope=scope,
        revision=1,
        created_at=now,
        name="test",
        adapter="kubernetes",
        external_identity="test-cluster",
        boundary="weave",
        runner_principal_id=uuid4(),
        capabilities=["observe", "scale_workers"],
    )
    component = ComponentSpec(
        name="worker",
        kind="worker",
        image="example.test/worker@sha256:" + "a" * 64,
        configuration="current",
        replicas=2,
        worker_release_id=uuid4(),
    )
    deployment = Deployment(
        id=uuid4(),
        scope=scope,
        revision=1,
        created_at=now,
        target_id=target.id,
        name="test",
        components=[component],
        ownership="managed",
    )
    observed = ObservedResource(
        name="worker",
        external_identity="deployment/worker",
        kind="worker",
        image=component.image,
        replicas=1,
        ready_replicas=1,
        version="8",
        ownership="imported",
        state="ready",
    )
    observation = DeploymentObservation(
        id=uuid4(),
        target_id=target.id,
        target_revision=1,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        digest="a" * 64,
        resources=[observed],
        complete=True,
    )
    observation = observation.model_copy(
        update={"digest": canonical_digest(observation.model_dump(mode="json", exclude={"digest"}))}
    )
    plan = DeploymentPlan(
        id=uuid4(),
        scope=scope,
        target_id=target.id,
        target_revision=1,
        deployment_id=deployment.id,
        deployment_revision=1,
        adapter=target.adapter,
        intent="scale_workers",
        observation_id=observation.id,
        observation_digest=observation.digest,
        steps=[PlanStep(action="scale_workers", component=component, expected_version="8")],
        risks=["external_effects"],
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        digest="a" * 64,
    )
    plan = plan.model_copy(update={"digest": canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))})
    job = DeploymentJob(
        id=uuid4(),
        scope=scope,
        target_id=target.id,
        target_revision=1,
        kind="apply",
        plan_id=plan.id,
        plan_digest=plan.digest,
        state="claimed",
        revision=1,
        operation_key=uuid4(),
        created_at=now,
        deadline=now + timedelta(seconds=180),
    )
    lease = DeploymentLease(
        proof=DeploymentLeaseProof(job_id=job.id, runner_id=uuid4(), generation=1, token=uuid4()),
        expires_at=now + timedelta(seconds=60),
        job=job,
        target=target,
        deployment=deployment,
        plan=plan,
        observation=observation,
    )
    policy = DestinationPolicy(
        target_id=target.id,
        adapter=target.adapter,
        external_identity="test-cluster",
        boundary="weave",
        executable="/usr/bin/kubectl",
        context="local",
        capabilities=target.capabilities,
        components=[{"name": "worker", "kind": "worker", "configuration": "current", "container": "worker"}],
        image_repositories=["example.test/worker"],
    )
    return lease, policy


def test_matching_plan_is_allowed_but_tampered_or_unbound_plan_is_rejected():
    lease, policy = fixture()
    validate_lease(lease, policy)
    for altered in [
        lease.model_copy(update={"target": lease.target.model_copy(update={"boundary": "other"})}),
        lease.model_copy(update={"plan": lease.plan.model_copy(update={"digest": "f" * 64})}),
        lease.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
    ]:
        with pytest.raises(RunnerPolicyError):
            validate_lease(altered, policy)


class Client:
    def __init__(self, lease):
        self.lease, self.reports = lease, []
        self.renewals = 0

    async def invoke(self, operation, **kwargs):
        if operation == "deployment_runners.report":
            self.reports.append(kwargs["body"])
            return self.lease.job
        if operation == "deployment_runners.renew":
            self.renewals += 1
            return self.lease
        raise AssertionError(operation)


class Adapter:
    def __init__(self, lease, fail=False):
        self.lease, self.fail, self.writes = lease, fail, 0

    async def observe(self):
        return ObservationReport(resources=self.lease.observation.resources, complete=True)

    async def apply(self, plan, before_write):
        await before_write()
        self.writes += 1
        if self.fail:
            raise RuntimeError("private provider diagnostic")
        return await self.observe()


@pytest.mark.asyncio
async def test_external_effects_start_only_after_running_report_and_renewal():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert [item.state for item in client.reports] == ["running", "verifying", "succeeded"]
    assert client.renewals >= 1
    assert adapter.writes == 1
    assert client.reports[-1].receipt.code == "applied"


@pytest.mark.asyncio
async def test_provider_failure_after_effect_is_ambiguous_and_never_retried():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease, fail=True)
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert adapter.writes == 1
    terminal = client.reports[-1]
    assert terminal.state == "reconciliation_required"
    assert terminal.receipt.external_effects_may_continue
    assert "private" not in terminal.model_dump_json()


@pytest.mark.asyncio
async def test_revoked_lease_during_work_cancels_the_owned_effect():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def apply(plan, before_write):
        await before_write()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter.apply = apply
    original = client.invoke

    async def invoke(operation, **kwargs):
        if operation == "deployment_runners.renew" and started.is_set():
            raise RuntimeError("revoked")
        return await original(operation, **kwargs)

    client.invoke = invoke
    await OperationsRunner(client, policy, adapter, renew_interval=0.01).execute(lease)
    assert cancelled.is_set()
    assert client.reports[-1].state == "reconciliation_required"


def test_a_restart_of_anything_but_a_worker_is_refused():
    lease, policy = fixture()
    api = ComponentSpec(
        name="worker", kind="api", image=lease.plan.steps[0].component.image, configuration="current", replicas=1
    )
    target = lease.target.model_copy(
        update={"adapter": "docker-compose", "capabilities": ["observe", "restart_workers"]}
    )
    plan = lease.plan.model_copy(
        update={
            "adapter": "docker-compose",
            "intent": "restart_workers",
            "steps": [PlanStep(action="restart_workers", component=api, expected_version="8")],
        }
    )
    plan = plan.model_copy(update={"digest": canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))})
    job = lease.job.model_copy(update={"plan_digest": plan.digest})
    deployment = lease.deployment.model_copy(update={"components": [api]})
    compose = policy.model_copy(
        update={
            "adapter": "docker-compose",
            "capabilities": ["observe", "restart_workers"],
            "compose_file": "/srv/compose.yaml",
            "lock_file": "/srv/runner.lock",
            "components": [policy.components[0].model_copy(update={"kind": "api", "container": None})],
        }
    )
    restart = lease.model_copy(update={"target": target, "plan": plan, "job": job, "deployment": deployment})
    with pytest.raises(RunnerPolicyError):
        validate_lease(restart, compose)
    # The same lease restarting a worker passes the check.
    worker = lease.plan.steps[0].component
    allowed = plan.model_copy(
        update={"steps": [PlanStep(action="restart_workers", component=worker, expected_version="8")]}
    )
    allowed = allowed.model_copy(
        update={"digest": canonical_digest(allowed.model_dump(mode="json", exclude={"digest"}))}
    )
    validate_lease(
        restart.model_copy(
            update={
                "plan": allowed,
                "job": job.model_copy(update={"plan_digest": allowed.digest}),
                "deployment": lease.deployment,
            }
        ),
        compose.model_copy(update={"components": policy.components}),
    )


@pytest.mark.parametrize(
    ("adapter", "version"), [("docker-compose", "2"), ("kubernetes", "2"), ("azure-container-apps", "1")]
)
def test_only_an_adapter_that_removes_the_reviewed_members_registers_version_2(adapter, version):
    """Version 2 promises the scale-down rule: Compose and Kubernetes remove exactly the reviewed members."""
    from types import SimpleNamespace
    from uuid import uuid4

    from firefly_weave.deployment_runner.application import runner_registration

    destination = SimpleNamespace(target_id=uuid4(), adapter=adapter, capabilities=["observe", "scale_workers"])
    sent = runner_registration(destination)
    assert (sent.adapter, sent.adapter_version, sent.capabilities) == (adapter, version, ["observe", "scale_workers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_a_report_the_platform_refuses_for_capacity_is_sent_again_unchanged(code, monkeypatch):
    """A busy platform answers 429 to a report; the runner sends the same report again instead of reporting the
    applied change as ambiguous."""
    from firefly_weave.deployment_runner import runtime
    from firefly_weave.sdk.errors import Problem, WeaveError

    monkeypatch.setattr(runtime, "REPORT_RETRY_SECONDS", 0.01)
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    original = client.invoke
    refused = []

    async def busy(operation, **kwargs):
        if operation == "deployment_runners.report" and kwargs["body"].state == "succeeded" and len(refused) < 2:
            refused.append(kwargs["body"])
            raise WeaveError(Problem(status=429, code=code, message="busy"))
        return await original(operation, **kwargs)

    client.invoke = busy
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert [item.state for item in client.reports] == ["running", "verifying", "succeeded"]
    assert {item.report_id for item in refused} == {client.reports[-1].report_id}
    assert client.reports[-1].receipt.code == "applied"


@pytest.mark.asyncio
async def test_a_failed_job_logs_where_it_failed_and_nothing_the_provider_said(caplog):
    """An operator reading the runner's log learns the job, its outcome and the code location that refused it, never
    a provider diagnostic."""
    import logging

    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease, fail=True)
    with caplog.at_level(logging.WARNING, logger="weave.deployment_runner"):
        await OperationsRunner(client, policy, adapter).execute(lease)
    (record,) = [item for item in caplog.records if item.name == "weave.deployment_runner"]
    message = record.getMessage()
    assert str(lease.job.id) in message and "reconciliation required" in message
    assert "RuntimeError at apply" in message
    assert "private provider diagnostic" not in message


@pytest.mark.asyncio
async def test_a_report_refused_for_any_other_reason_is_not_sent_again(monkeypatch):
    """Only a capacity refusal is sent again; another 429 (or any refusal) fails the job as before."""
    from firefly_weave.deployment_runner import runtime
    from firefly_weave.sdk.errors import Problem, WeaveError

    monkeypatch.setattr(runtime, "REPORT_RETRY_SECONDS", 0.01)
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    original = client.invoke
    refused = []

    async def limited(operation, **kwargs):
        if operation == "deployment_runners.report" and kwargs["body"].state == "succeeded":
            refused.append(kwargs["body"])
            raise WeaveError(Problem(status=429, code="WV-RATE-LIMITED", message="slow down"))
        return await original(operation, **kwargs)

    client.invoke = limited
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert len(refused) == 1
    assert client.reports[-1].state == "reconciliation_required"


def served(monkeypatch):
    """Replace the SDK client serve() builds: record how it was built, register, and claim nothing."""
    from types import SimpleNamespace

    from firefly_weave.deployment_runner import application

    built = []

    class Client:
        def __init__(self, base_url, provider, scope, *, timeout, transport=None, private_origin=None):
            built.append({"base_url": base_url, "transport": transport, "private_origin": private_origin})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, *, body=None):
            if operation == "deployment_runners.create":
                return SimpleNamespace(id=uuid4())
            assert operation == "deployment_runners.claim"
            return None

    monkeypatch.setattr(application, "WeaveClient", Client)
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    monkeypatch.delenv("WEAVE_RUNNER_HEARTBEAT_FILE", raising=False)
    return built


def runner_configuration(base_url):
    from firefly_weave.deployment_runner.application import RunnerConfiguration

    return RunnerConfiguration.model_validate(
        {
            "base_url": base_url,
            "scope": Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
            "token_file": "/run/weave/secrets/token",
            "destination": DestinationPolicy.model_validate_json(
                __import__("json").dumps(
                    {
                        "target_id": str(uuid4()),
                        "adapter": "kubernetes",
                        "external_identity": "namespace-uid",
                        "boundary": "weave-workers",
                        "executable": "/usr/local/bin/kubectl",
                        "context": "weave-runner",
                        "capabilities": ["observe"],
                        "components": [
                            {"name": "weave-worker", "container": "worker", "kind": "worker", "configuration": "local"}
                        ],
                        "image_repositories": ["127.0.0.1:5000/example-sleep"],
                    }
                )
            ),
        }
    )


async def test_the_runner_uses_the_pinned_transport_for_a_plain_http_api(monkeypatch):
    from firefly_weave import private_origins as po
    from firefly_weave.deployment_runner.application import serve

    built = served(monkeypatch)
    origin = "http://weave-api.weave-system.svc:8000"
    entry = po.PrivateOrigin(origin=origin, purpose="runner", networks=("10.89.0.0/24",), credentials="bridge")
    with po.installed(po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])):
        for base_url in (origin, "https://weave.example", "http://127.0.0.1:8000"):
            await serve(runner_configuration(base_url), once=True)
    # Only the approved private origin gets the DNS-pinned transport; HTTPS and loopback stay as they were.
    assert [(item["transport"] is not None, item["private_origin"]) for item in built] == [
        (True, "runner"),
        (False, None),
        (False, None),
    ]


async def test_the_runner_writes_its_heartbeat_after_a_claim(monkeypatch, tmp_path):
    import stat

    from firefly_weave.deployment_runner.application import serve

    served(monkeypatch)
    beat = tmp_path / "heartbeat"
    monkeypatch.setenv("WEAVE_RUNNER_HEARTBEAT_FILE", str(beat))
    await serve(runner_configuration("https://weave.example"), once=True)
    assert stat.S_IMODE(beat.stat().st_mode) == 0o600


async def test_lease_renewals_during_a_job_count_as_contact():
    """A restart can run 330 s, longer than the liveness probe allows, so every renewal refreshes the heartbeat."""
    lease, policy = fixture()
    client, adapter, contacts = Client(lease), Adapter(lease), []
    await OperationsRunner(client, policy, adapter, on_contact=lambda: contacts.append(True)).execute(lease)
    assert client.renewals >= 1 and len(contacts) == client.renewals


def claiming(monkeypatch, answers):
    """Like served(), but the runner's claims get `answers` in order (an exception to raise, or a lease), then no
    work; returns the list of claims it made. Waits before claiming again are cut to a hundredth of a second."""
    from types import SimpleNamespace

    from firefly_weave.deployment_runner import application

    pending, claims = list(answers), []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, *, body=None):
            if operation == "deployment_runners.create":
                return SimpleNamespace(id=uuid4())
            claims.append(operation)
            answer = pending.pop(0) if pending else None
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(application, "WeaveClient", Client)
    monkeypatch.setattr(application, "CLAIM_RETRY_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(application, "MAX_CLAIM_RETRY_SECONDS", 0.01, raising=False)
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    monkeypatch.delenv("WEAVE_RUNNER_HEARTBEAT_FILE", raising=False)
    return claims


def through_the_client(monkeypatch, transport):
    """Like claiming(), but each claim goes through the real WeaveClient over `transport` (None keeps the client's own
    network transport), so the runner sees exactly the errors the client raises. Registration is faked, and the token
    comes from memory; returns the list of claims the runner made."""
    from types import SimpleNamespace

    from firefly_weave.deployment_runner import application
    from firefly_weave.sdk.client import WeaveClient

    claims = []

    class Token:
        async def get_access_token(self, target):
            return "token"

    class Client(WeaveClient):
        def __init__(self, base_url, provider, scope, **options):
            super().__init__(base_url, Token(), scope, **(options | {"transport": transport or options["transport"]}))

        async def invoke(self, operation, **arguments):
            if operation == "deployment_runners.create":
                return SimpleNamespace(id=uuid4())
            claims.append(operation)
            return await super().invoke(operation, **arguments)

    monkeypatch.setattr(application, "WeaveClient", Client)
    monkeypatch.setattr(application, "CLAIM_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(application, "MAX_CLAIM_RETRY_SECONDS", 0.01)
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    monkeypatch.delenv("WEAVE_RUNNER_HEARTBEAT_FILE", raising=False)
    return claims


def answering(*answers):
    """A transport that answers claims with `answers` in order (an exception to raise or a response), then no work."""
    import httpx

    pending = list(answers)

    def answer(request):
        reply = pending.pop(0) if pending else httpx.Response(200, content=b"null")
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return httpx.MockTransport(answer)


def unreachable_claims():
    import httpcore
    import httpx

    return [
        # WeaveClient reports a refused, dropped or timed-out connection on HTTPS or loopback as WV-TRANSPORT.
        pytest.param(lambda: httpx.ConnectError("refused"), id="refused"),
        pytest.param(lambda: httpx.RemoteProtocolError("closed"), id="dropped-mid-response"),
        pytest.param(lambda: httpx.ReadTimeout("slow"), id="timed-out"),
        # In a cluster the pinned private-origin transport passes httpcore's error through WeaveClient.
        pytest.param(lambda: httpcore.ConnectError("refused"), id="refused-pinned-transport"),
        # A gateway in front of an API that is not serving yet.
        pytest.param(lambda: httpx.Response(503, text="<html>unavailable</html>"), id="http-503"),
        pytest.param(lambda: httpx.Response(502, text="<html>bad gateway</html>"), id="http-502"),
    ]


@pytest.mark.parametrize("unreachable", unreachable_claims())
async def test_the_runner_claims_again_once_the_api_answers_after_it_could_not_be_reached(monkeypatch, unreachable):
    from firefly_weave.deployment_runner.application import serve

    claims = through_the_client(monkeypatch, answering(unreachable(), unreachable()))
    running = asyncio.create_task(serve(runner_configuration("https://weave.example")))
    for _ in range(200):
        if len(claims) >= 3 or running.done():
            break
        await asyncio.sleep(0.01)
    stopped_early = running.done() and not running.cancelled() and running.exception()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert not stopped_early, f"serve() ended on a claim the API could not answer: {stopped_early!r}"
    assert len(claims) >= 3


async def test_the_runner_stops_on_a_claim_the_api_refuses_once_it_answers(monkeypatch):
    import httpx

    from firefly_weave.deployment_runner.application import serve
    from firefly_weave.sdk.errors import WeaveError

    for status in (401, 403, 404):
        refusal = httpx.Response(status, json={"status": status, "code": "WV-REFUSED", "message": "Refused"})
        claims = through_the_client(monkeypatch, answering(httpx.ConnectError("refused"), refusal))
        with pytest.raises(WeaveError) as refused:
            await asyncio.wait_for(serve(runner_configuration("https://weave.example")), 2)
        assert refused.value.status == status and len(claims) == 2


async def test_a_runner_asked_to_claim_once_never_claims_again(monkeypatch):
    # `--once` checks one claim and reports how it went, so even a claim that could not reach the API ends it.
    import httpx

    from firefly_weave.deployment_runner.application import serve
    from firefly_weave.sdk.errors import TransportError

    claims = through_the_client(monkeypatch, answering(httpx.ConnectError("refused")))
    with pytest.raises(TransportError):
        await asyncio.wait_for(serve(runner_configuration("https://weave.example"), once=True), 2)
    assert claims == ["deployment_runners.claim"]


def client_refusals():
    import httpx

    from firefly_weave.connectors.egress import EgressDenied

    return [
        # The pinned transport refuses a destination its private-origin policy does not permit.
        pytest.param(lambda: EgressDenied(), id="destination-refused"),
        pytest.param(lambda: httpx.UnsupportedProtocol("Request URL has an unsupported protocol"), id="protocol"),
        pytest.param(
            lambda: httpx.Response(200, content=b"null", headers={"X-Weave-Wire-Version": "weave/api-v9"}),
            id="wire-version",
        ),
        pytest.param(lambda: httpx.Response(200, json={"not": "a lease"}), id="not-the-contract"),
        # The API never redirects, so a redirect means the runner was given the wrong address.
        pytest.param(lambda: httpx.Response(307, headers={"Location": "https://elsewhere.example/"}), id="redirect"),
    ]


@pytest.mark.parametrize("refusal", client_refusals())
async def test_the_runner_stops_on_a_claim_its_own_client_refused(monkeypatch, refusal):
    """Waiting fixes none of these: the client refused the destination, the protocol or the answer itself."""
    from firefly_weave.deployment_runner.application import serve
    from firefly_weave.sdk.errors import WeaveError

    claims = through_the_client(monkeypatch, answering(refusal()))
    with pytest.raises(WeaveError):
        await asyncio.wait_for(serve(runner_configuration("https://weave.example")), 2)
    assert claims == ["deployment_runners.claim"]


async def untrusted_api(folder, received):
    """A real HTTPS server on 127.0.0.1 whose self-signed certificate nobody trusts; `received` gets every byte a
    request sends it."""
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        # The address matches, so the only fault is that nobody vouches for the certificate.
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    pem = folder / "api.pem"
    pem.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)

    async def answer(reader, writer):
        received.extend(await reader.read(65536))
        writer.close()

    return await asyncio.start_server(answer, "127.0.0.1", 0, ssl=context)


async def test_the_runner_stops_on_a_certificate_it_cannot_verify(monkeypatch, tmp_path):
    """Waiting never makes a certificate trusted: the runner stops at once instead of retrying, and no claim, with
    its token, ever reached that server."""
    from firefly_weave.deployment_runner.application import serve
    from firefly_weave.sdk.errors import TransportError

    received = bytearray()
    server = await untrusted_api(tmp_path, received)
    claims = through_the_client(monkeypatch, None)
    try:
        with pytest.raises(TransportError):
            await asyncio.wait_for(
                serve(runner_configuration(f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}")), 2
            )
    finally:
        server.close()
        await server.wait_closed()
    assert claims == ["deployment_runners.claim"] and not received


async def test_a_cancellation_that_a_failed_runner_claim_hid_still_stops_the_runner(monkeypatch):
    import httpcore

    from firefly_weave.deployment_runner import application

    claiming(monkeypatch, [])
    claiming_now, claims = asyncio.Event(), []

    class Client(application.WeaveClient):
        async def invoke(self, operation, *, body=None):
            if operation == "deployment_runners.create":
                return await super().invoke(operation, body=body)
            claims.append(operation)
            claiming_now.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A client library that closes its connection on cancellation can raise its own error instead.
                raise httpcore.ConnectError("connection closed") from None

    monkeypatch.setattr(application, "WeaveClient", Client)
    running = asyncio.create_task(application.serve(runner_configuration("https://weave.example")))
    await asyncio.wait_for(claiming_now.wait(), 1)
    running.cancel()
    await asyncio.wait({running}, timeout=1)
    if not running.done():
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert running.cancelled() and claims == ["deployment_runners.claim"]


async def test_the_runner_starts_the_token_relay_when_asked(monkeypatch):
    from firefly_weave.deployment_runner import application

    served(monkeypatch)
    started, closed = [], []

    class Relay:
        def close(self):
            closed.append(True)

        async def wait_closed(self):
            return None

    async def start_relay(destination, *, listen=8080):
        started.append((destination, listen))
        return Relay()

    monkeypatch.setattr(application, "start_relay", start_relay)
    monkeypatch.setenv("WEAVE_TOKEN_RELAY", "weave-api.weave-system.svc:8080")
    await application.serve(runner_configuration("https://weave.example"), once=True)
    assert started == [("weave-api.weave-system.svc:8080", 8080)] and closed == [True]


def scale_down_lease():
    """The fixture's lease as a reviewed scale-down from 3 workers to 1."""
    lease, policy = fixture()
    observed = lease.observation.resources[0].model_copy(update={"replicas": 3, "ready_replicas": 3})
    observation = lease.observation.model_copy(update={"resources": [observed]})
    observation = observation.model_copy(
        update={"digest": canonical_digest(observation.model_dump(mode="json", exclude={"digest"}))}
    )
    step = lease.plan.steps[0]
    component = step.component.model_copy(update={"replicas": 1})
    plan = lease.plan.model_copy(
        update={"steps": [step.model_copy(update={"component": component})], "observation_digest": observation.digest}
    )
    plan = plan.model_copy(update={"digest": canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))})
    job = lease.job.model_copy(update={"plan_digest": plan.digest})
    deployment = lease.deployment.model_copy(update={"components": [component]})
    return lease.model_copy(
        update={"plan": plan, "observation": observation, "job": job, "deployment": deployment}
    ), policy


class Cluster:
    """kubectl for the fixture's namespace: three ready worker pods, and no permission to patch pods."""

    def __init__(self, image, target_id):
        self.image, self.target_id, self.writes, self.dry_pod_patches = image, str(target_id), [], 0

    def deployment(self, replicas):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "worker",
                "namespace": "weave",
                "uid": "deployment-uid",
                "resourceVersion": "8",
                "generation": 1,
                "labels": {"firefly.weave/target": self.target_id},
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": "worker"}},
                "template": {
                    "metadata": {"annotations": {"firefly.weave/configuration": "current"}},
                    "spec": {"containers": [{"name": "worker", "image": self.image}]},
                },
            },
            "status": {"observedGeneration": 1, "replicas": 3, "readyReplicas": 3, "updatedReplicas": 3},
        }

    async def command(self, argv, **kwargs):
        import json

        from firefly_weave.deployment_runner.command import CommandError

        args = argv[6:]
        if args[:2] == ["get", "namespace"]:
            return json.dumps({"metadata": {"name": "weave", "uid": "test-cluster"}}).encode()
        if args[:2] == ["get", "deployments.apps"]:
            value = self.deployment(3)
            value["metadata"]["labels"] = {}
            return json.dumps(value).encode()
        if args[:2] == ["get", "pods"] and "?(@.metadata.annotations" in args[-1]:
            return b""
        if args[:2] == ["get", "pods"]:
            fields = ["worker-abc12-{0}", "weave", "", "abc12", "apps/v1", "ReplicaSet", "worker-abc12", "Running"]
            line = "\t".join([*fields, "True", "0", "2026-11-11T10:0{0}:00Z", "node"]) + "\n"
            return "".join(line.format(index) for index in range(3)).encode()
        if args[:2] == ["patch", "deployments.apps"]:
            if "--dry-run=server" not in args:
                self.writes.append(args)
            return json.dumps(self.deployment(1)).encode()
        if args[:2] == ["patch", "pods"]:
            self.dry_pod_patches += "--dry-run=server" in args
            raise CommandError()
        raise AssertionError(args)


async def test_a_scale_down_without_permission_to_patch_pods_fails_before_any_change():
    """A Kubernetes runner set up before scale-downs may lack patch on pods: the job fails, nothing changed."""
    from firefly_weave.deployment_runner.kubernetes import KubernetesAdapter

    lease, policy = scale_down_lease()
    client, cluster = Client(lease), Cluster(lease.plan.steps[0].component.image, policy.target_id)
    await OperationsRunner(client, policy, KubernetesAdapter(policy, command=cluster.command)).execute(lease)
    terminal = client.reports[-1]
    assert [item.state for item in client.reports] == ["running", "failed"]
    assert terminal.receipt.code == "precondition_failed" and not terminal.receipt.external_effects_may_continue
    assert cluster.writes == [] and cluster.dry_pod_patches == 1


async def test_a_heartbeat_that_cannot_be_written_never_fails_a_job(caplog):
    import logging

    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)

    def contact():
        raise PermissionError(13, "Permission denied")

    with caplog.at_level(logging.WARNING):
        await OperationsRunner(client, policy, adapter, on_contact=contact).execute(lease)
    assert client.reports[-1].state == "succeeded" and client.renewals >= 1
    assert any("heartbeat" in record.getMessage() for record in caplog.records)


async def test_the_runner_keeps_claiming_when_its_heartbeat_cannot_be_written(monkeypatch, tmp_path, caplog):
    import logging

    from firefly_weave.deployment_runner.application import serve

    served(monkeypatch)
    monkeypatch.setenv("WEAVE_RUNNER_HEARTBEAT_FILE", str(tmp_path / "missing" / "heartbeat"))
    with caplog.at_level(logging.WARNING):
        await serve(runner_configuration("https://weave.example"), once=True)
    assert any("heartbeat" in record.getMessage() for record in caplog.records)
