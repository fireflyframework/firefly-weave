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

"""The runner reads container logs with fixed argument lists, only for worker containers it observed itself, and
reports them redacted and cut."""

import json
from uuid import uuid4

import pytest

from firefly_weave.deployment_runner.command import CommandError, CommandOutput
from firefly_weave.deployment_runner.compose import ComposeAdapter
from firefly_weave.deployment_runner.kubernetes import KubernetesAdapter
from firefly_weave.deployment_runner.logs import LogRefused
from firefly_weave.deployment_runner.policy import DestinationPolicy

IMAGE = "example.test/worker@sha256:" + "b" * 64
# Secret-shaped values are built from parts at run time, so the repository holds no secret-shaped text.
TOKEN = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "c2lnbmF0dXJlLXBhcnQ"
RUNNING, EXITED, OTHER = "a" * 64, "c" * 64, "e" * 64


def compose_policy(tmp_path) -> DestinationPolicy:
    source = tmp_path / "compose.yaml"
    source.write_text("services: {}")
    return DestinationPolicy(
        target_id=uuid4(),
        adapter="docker-compose",
        external_identity="daemon-id",
        boundary="weave-test",
        context="owned-test",
        executable="/usr/bin/docker",
        capabilities=["observe", "read_logs"],
        compose_file=str(source),
        lock_file=str(tmp_path / "runner.lock"),
        components=[
            {"name": "worker", "kind": "worker", "configuration": "current"},
            {"name": "api", "kind": "api", "configuration": "current"},
        ],
        image_repositories=["example.test/worker"],
    )


def compose(config, containers):
    """A Compose daemon whose `worker` service runs `containers` (ID, state); `api` has none."""
    calls = []

    async def command(argv, **kwargs):
        calls.append(argv)
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            return json.dumps({"services": {"worker": {"image": IMAGE}, "api": {"image": IMAGE}}}).encode()
        if "ps" in argv:
            return json.dumps([{"ID": identifier, "Service": "worker"} for identifier, _ in containers]).encode()
        identifier = argv[-1]
        state = dict(containers)[identifier]
        return json.dumps(
            {
                "id": identifier,
                "image": IMAGE,
                "state": state,
                "started": "2026-11-19T10:00:00Z",
                "restart": 0,
                "project": "weave-test",
                "service": "worker",
                "target": str(config.target_id),
                "health": "",
            }
        ).encode()

    return command, calls


def printed(stdout=b"", stderr=b"", *, cut=False):
    """A read recorder that answers every container with the same output."""
    reads = []

    async def output(argv, **kwargs):
        reads.append(argv)
        return CommandOutput(stdout=stdout, stderr=stderr, cut=cut)

    return output, reads


STDOUT = (
    b"2026-11-19T10:41:52.123456789Z Worker ready: 4 slots, task types sql.lookup\n"
    b"2026-11-19T10:42:01.500000000Z Reported failure SMTP_REJECTED for task send\n"
)
STDERR = b"2026-11-19T10:42:01.000000000Z WARN smtp-relay answered 550\n"


@pytest.mark.asyncio
async def test_compose_reads_an_observed_container_with_a_fixed_argument_list(tmp_path):
    config = compose_policy(tmp_path)
    command, _ = compose(config, [(RUNNING, "running"), (EXITED, "exited")])
    output, reads = printed(STDOUT, STDERR)
    members, cut = await ComposeAdapter(config, command=command, output=output).read_logs(
        "worker", [RUNNING[:12]], 900, 200
    )
    assert reads == [
        [
            "/usr/bin/docker",
            "--context",
            "owned-test",
            "logs",
            "--timestamps",
            "--since",
            "900s",
            "--tail",
            "200",
            RUNNING,
        ]
    ]
    assert [member.member for member in members] == [RUNNING[:12]] and cut is False
    lines = members[0].lines
    assert [(line.stream, line.text) for line in lines] == [
        ("stdout", "Worker ready: 4 slots, task types sql.lookup"),
        ("stderr", "WARN smtp-relay answered 550"),
        ("stdout", "Reported failure SMTP_REJECTED for task send"),
    ]
    assert lines[0].at.isoformat() == "2026-11-19T10:41:52.123456+00:00"


@pytest.mark.asyncio
async def test_compose_reads_every_container_of_the_service_up_to_five_when_none_is_named(tmp_path):
    config = compose_policy(tmp_path)
    identifiers = [f"{index:x}" * 64 for index in range(1, 8)]
    # An exited container is read too: it may show why a worker crashed before it claimed anything.
    command, _ = compose(
        config, [(identifier, "exited" if index == 0 else "running") for index, identifier in enumerate(identifiers)]
    )
    output, reads = printed(STDOUT)
    members, cut = await ComposeAdapter(config, command=command, output=output).read_logs("worker", [], 60, 10)
    assert len(members) == len(reads) == 5 and cut is True
    assert all(argv[-1] in identifiers for argv in reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["api", "unknown", "worker --since=1s"])
async def test_a_component_that_isnt_a_bound_worker_is_refused_before_any_command(tmp_path, component):
    config = compose_policy(tmp_path)
    command, calls = compose(config, [(RUNNING, "running")])
    output, reads = printed(STDOUT)
    with pytest.raises(LogRefused) as refused:
        await ComposeAdapter(config, command=command, output=output).read_logs(component, [], 900, 200)
    assert refused.value.failure == "unknown_component"
    assert calls == [] and reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [OTHER[:12], "aaaaaaaaaaa", RUNNING, "--privileged"])
async def test_a_member_the_runner_didnt_observe_is_never_read(tmp_path, member):
    config = compose_policy(tmp_path)
    command, _ = compose(config, [(RUNNING, "running")])
    output, reads = printed(STDOUT)
    with pytest.raises(LogRefused) as refused:
        await ComposeAdapter(config, command=command, output=output).read_logs("worker", [member], 900, 200)
    assert refused.value.failure == "no_members" and reads == []


@pytest.mark.asyncio
async def test_lines_over_2_kib_and_snapshots_over_256_kib_are_cut_and_say_so(tmp_path):
    config = compose_policy(tmp_path)
    identifiers = [f"{index:x}" * 64 for index in range(1, 6)]
    command, _ = compose(config, [(identifier, "running") for identifier in identifiers])
    # Each container printed 999 lines of 1,500 bytes, then its newest line of 6,000 bytes: 7.5 MB in all.
    many = b"".join(b"2026-11-19T10:%02d:%02dZ " % divmod(n, 60) + b"x" * 1500 + b"\n" for n in range(999))
    long_line = b"2026-11-19T10:41:52Z " + "é".encode() * 3000 + b"\n"
    output, _ = printed(many + long_line)
    members, cut = await ComposeAdapter(config, command=command, output=output).read_logs("worker", [], 3600, 1000)
    lines = [line for member in members for line in member.lines]
    assert cut is True
    # The long line is cut at a character within 2 KiB; the snapshot keeps the newest lines within 256 KiB.
    assert [member.lines[-1].text for member in members] == ["é" * 1024] * 5
    assert all(len(line.text.encode()) <= 2048 for line in lines)
    assert 250 * 1024 < sum(len(line.text.encode()) for line in lines) <= 256 * 1024
    oldest = min(line.at for line in lines)
    assert oldest.minute == 16 and all(len(member.lines) < 999 for member in members)


@pytest.mark.asyncio
async def test_the_runner_redacts_before_it_reports(tmp_path):
    config = compose_policy(tmp_path)
    command, _ = compose(config, [(RUNNING, "running")])
    output, _ = printed(b"2026-11-19T10:41:52Z calling relay with Authorization: Bearer " + TOKEN.encode() + b"\n")
    members, _ = await ComposeAdapter(config, command=command, output=output).read_logs("worker", [], 900, 200)
    text = members[0].lines[0].text
    assert TOKEN not in text and "[redacted]" in text


@pytest.mark.asyncio
async def test_a_line_without_its_time_is_left_out_and_a_failed_read_is_unreadable(tmp_path):
    config = compose_policy(tmp_path)
    command, _ = compose(config, [(RUNNING, "running")])
    output, _ = printed(b"no time here\n2026-11-19T10:41:52Z kept\n")
    members, cut = await ComposeAdapter(config, command=command, output=output).read_logs("worker", [], 900, 200)
    assert [line.text for line in members[0].lines] == ["kept"] and cut is True

    async def failing(argv, **kwargs):
        raise CommandError()

    with pytest.raises(LogRefused) as refused:
        await ComposeAdapter(config, command=command, output=failing).read_logs("worker", [], 900, 200)
    assert refused.value.failure == "unreadable"


def kubernetes_policy() -> DestinationPolicy:
    return DestinationPolicy(
        target_id=uuid4(),
        adapter="kubernetes",
        external_identity="namespace-uid",
        boundary="weave-workers",
        executable="/usr/local/bin/kubectl",
        context="weave-runner",
        capabilities=["observe", "read_logs"],
        components=[{"name": "weave-worker", "kind": "worker", "configuration": "local", "container": "worker"}],
        image_repositories=["example.test/worker"],
    )


DEPLOYMENT = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "weave-worker", "namespace": "weave-workers", "uid": "u", "resourceVersion": "8"},
    "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "weave-worker"}}},
}


def pod_line(name: str, phase: str = "Running") -> str:
    fields = [name, "weave-workers", "", "7c9f", "apps/v1", "ReplicaSet", "weave-worker-7c9f", phase, "True", "0"]
    return "\t".join([*fields, "2026-11-19T10:00:00Z", "node-1"]) + "\n"


def cluster(pods: str):
    async def command(argv, **kwargs):
        assert argv[:6] == [
            "/usr/local/bin/kubectl",
            "--context",
            "weave-runner",
            "--namespace",
            "weave-workers",
            "--request-timeout=20s",
        ]
        if argv[6:8] == ["get", "namespace"]:
            return json.dumps({"metadata": {"name": "weave-workers", "uid": "namespace-uid"}}).encode()
        if "deployments.apps" in argv:
            return json.dumps(DEPLOYMENT).encode()
        assert argv[6:8] == ["get", "pods"]
        return pods.encode()

    return command


@pytest.mark.asyncio
async def test_kubernetes_reads_a_listed_running_pod_with_a_fixed_argument_list():
    pods = pod_line("weave-worker-7c9f-x2k4") + pod_line("weave-worker-7c9f-p8sd", "Pending")
    output, reads = printed(STDOUT)
    adapter = KubernetesAdapter(kubernetes_policy(), command=cluster(pods), output=output)
    members, cut = await adapter.read_logs("weave-worker", [], 900, 200)
    # A pending pod has no logs yet, so only the running one is read.
    assert reads == [
        [
            "/usr/local/bin/kubectl",
            "--context",
            "weave-runner",
            "--namespace",
            "weave-workers",
            "--request-timeout=20s",
            "logs",
            "weave-worker-7c9f-x2k4",
            "-c",
            "worker",
            "--timestamps",
            "--since=900s",
            "--tail=200",
        ]
    ]
    assert [line.stream for line in members[0].lines] == ["output", "output"] and cut is False
    with pytest.raises(LogRefused) as refused:
        await adapter.read_logs("weave-worker", ["weave-worker-other"], 900, 200)
    assert refused.value.failure == "no_members"


def configuration():
    from firefly_weave.contracts.access import Scope
    from firefly_weave.deployment_runner.application import RunnerConfiguration

    return RunnerConfiguration(
        base_url="https://weave.example",
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        token_file="/run/weave/secrets/token",
        destination=kubernetes_policy(),
        poll_seconds=1,
    )


def log_claim(config, **values):
    from datetime import UTC, datetime, timedelta

    from firefly_weave.contracts.deployments import DeploymentTarget, LogClaim, LogLeaseProof

    policy = config.destination
    now = datetime.now(UTC)
    target = DeploymentTarget(
        id=policy.target_id,
        scope=config.scope,
        revision=1,
        created_at=now,
        name="kind-weave-test",
        adapter=policy.adapter,
        external_identity=policy.external_identity,
        boundary=policy.boundary,
        runner_principal_id=uuid4(),
        capabilities=["observe", "read_logs"],
    )
    return LogClaim(
        proof=LogLeaseProof(request_id=uuid4(), runner_id=uuid4(), token=uuid4()),
        expires_at=now + timedelta(seconds=60),
        target=values.pop("target", target),
        component="weave-worker",
        members=[],
        since_seconds=900,
        tail_lines=200,
        **values,
    )


class Reader:
    """A log reader standing in for the adapter: what it read, or the error it raises."""

    def __init__(self, members=None, error=None):
        self.members, self.error, self.calls = members or [], error, []

    async def read_logs(self, component, members, since_seconds, tail_lines):
        self.calls.append((component, members, since_seconds, tail_lines))
        if self.error is not None:
            raise self.error
        return self.members, False


@pytest.mark.asyncio
async def test_a_log_request_never_waits_behind_a_job(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from firefly_weave.contracts.deployments import LogLine, MemberLog
    from firefly_weave.deployment_runner import application

    config = configuration()
    claim = log_claim(config)
    job_running, job_released, reported = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reports, claims = [], {"jobs": 0, "logs": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, *, body=None):
            if operation == "deployment_runners.create":
                return SimpleNamespace(id=claim.proof.runner_id, capabilities=body.capabilities)
            if operation == "deployment_runners.claim":
                claims["jobs"] += 1
                return object() if claims["jobs"] == 1 else None
            if operation == "deployment_runners.claim_logs":
                claims["logs"] += 1
                return claim if claims["logs"] == 1 else None
            assert operation == "deployment_runners.report_logs"
            reports.append(body)
            reported.set()
            return SimpleNamespace(request_id=claim.proof.request_id, state="ready")

    async def execute(self, lease):
        job_running.set()
        await job_released.wait()

    line = LogLine(at=claim.expires_at, stream="output", text="Worker ready")
    monkeypatch.setattr(application, "WeaveClient", Client)
    monkeypatch.setattr(application.OperationsRunner, "execute", execute)
    monkeypatch.setattr(application, "adapter_for", lambda policy: Reader([MemberLog(member="w1", lines=[line])]))
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    monkeypatch.delenv("WEAVE_RUNNER_HEARTBEAT_FILE", raising=False)
    serving = asyncio.create_task(application.serve(config))
    try:
        await asyncio.wait_for(job_running.wait(), 2)
        # The job is still running when the log request is claimed, read and reported.
        await asyncio.wait_for(reported.wait(), 2)
        assert not job_released.is_set()
        assert reports[0].proof == claim.proof and reports[0].members[0].lines == [line]
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving


@pytest.mark.asyncio
async def test_a_target_without_read_logs_keeps_its_runner_working_without_log_requests(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from firefly_weave.deployment_runner import application
    from firefly_weave.sdk.errors import Problem, WeaveError

    registered, operations = [], []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, *, body=None):
            operations.append(operation)
            if operation == "deployment_runners.create":
                registered.append(list(body.capabilities))
                # A target registered before container logs refuses a runner that offers read_logs.
                if "read_logs" in body.capabilities:
                    raise WeaveError(Problem(status=403, code="WV-FORBIDDEN", message="Forbidden"))
                return SimpleNamespace(id=uuid4(), capabilities=body.capabilities)
            assert operation == "deployment_runners.claim"
            return None

    monkeypatch.setattr(application, "WeaveClient", Client)
    monkeypatch.setattr(application, "adapter_for", lambda policy: Reader())
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    monkeypatch.delenv("WEAVE_RUNNER_HEARTBEAT_FILE", raising=False)
    config = configuration().model_copy(update={"poll_seconds": 0.01})
    serving = asyncio.create_task(application.serve(config))
    try:
        while operations.count("deployment_runners.claim") < 3:
            await asyncio.sleep(0.01)
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
    assert registered == [["observe", "read_logs"], ["observe"]]
    assert "deployment_runners.claim_logs" not in operations
    # Any other refusal still stops the runner.

    class Refusing(Client):
        async def invoke(self, operation, *, body=None):
            raise WeaveError(Problem(status=403, code="WV-FORBIDDEN", message="Forbidden"))

    monkeypatch.setattr(application, "WeaveClient", Refusing)
    with pytest.raises(WeaveError):
        await asyncio.wait_for(application.serve(config), 2)


@pytest.mark.asyncio
async def test_a_claim_for_another_target_or_a_failed_read_reports_why_and_logs_no_line(caplog):
    from firefly_weave.deployment_runner.runtime import OperationsRunner

    config = configuration()
    sent = []

    class Client:
        async def invoke(self, operation, *, body=None):
            sent.append((operation, body))

    foreign = log_claim(config).target.model_copy(update={"external_identity": "another-namespace"})
    reader = Reader(error=CommandError())
    runner = OperationsRunner(Client(), config.destination, reader)
    await runner.serve_logs(log_claim(config, target=foreign))
    assert reader.calls == []
    await runner.serve_logs(log_claim(config))
    secret = Reader(members=[], error=LogRefused("no_members"))
    await OperationsRunner(Client(), config.destination, secret).serve_logs(log_claim(config))
    assert [(operation, body.failure, body.members) for operation, body in sent] == [
        ("deployment_runners.report_logs", "unreadable", []),
        ("deployment_runners.report_logs", "unreadable", []),
        ("deployment_runners.report_logs", "no_members", []),
    ]
    assert "Worker" not in caplog.text and TOKEN not in caplog.text


def test_compose_and_kubernetes_runners_may_read_logs_and_container_apps_runners_may_not(tmp_path):
    assert "read_logs" in compose_policy(tmp_path).capabilities
    assert "read_logs" in kubernetes_policy().capabilities
    container_apps = kubernetes_policy().model_dump(mode="json") | {
        "adapter": "azure-container-apps",
        "lock_file": str(tmp_path / "runner.lock"),
    }
    DestinationPolicy.model_validate_json(json.dumps({**container_apps, "capabilities": ["observe"]}))
    with pytest.raises(ValueError):
        DestinationPolicy.model_validate_json(json.dumps(container_apps))


SH = "/bin/sh"


@pytest.mark.asyncio
async def test_reading_output_keeps_both_streams_apart():
    from firefly_weave.deployment_runner.command import read_output

    printed = await read_output([SH, "-c", "printf 'one\\ntwo\\n'; printf 'oops\\n' >&2"])
    assert (printed.stdout, printed.stderr, printed.cut) == (b"one\ntwo\n", b"oops\n", False)


@pytest.mark.asyncio
async def test_reading_output_keeps_the_last_whole_lines_and_says_what_it_left_out():
    from firefly_weave.deployment_runner.command import read_output

    script = "i=0; while [ $i -lt 200 ]; do printf 'line %03d\\n' $i; i=$((i+1)); done"
    printed = await read_output([SH, "-c", script], keep=100)
    assert printed.cut is True and printed.stdout.endswith(b"line 199\n")
    assert all(line.startswith(b"line ") and len(line) == 8 for line in printed.stdout.splitlines())
    # Past its read limit the command is stopped, and what was read is kept without the line the stop cut.
    endless = await read_output([SH, "-c", "while :; do printf 'again and again\\n'; done"], keep=4096, limit=65536)
    assert endless.cut is True and endless.stdout.endswith(b"again and again\n")


@pytest.mark.asyncio
async def test_reading_output_fails_for_a_failed_command_or_an_argument_list_it_wont_run():
    from firefly_weave.deployment_runner.command import read_output

    for argv in ([SH, "-c", "exit 3"], ["sh", "-c", "true"], [SH, "-c", "true\0"]):
        with pytest.raises(CommandError):
            await read_output(argv)
