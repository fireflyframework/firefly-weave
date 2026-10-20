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

"""weave platform up sets up local operations out of the box: resumable, exact, and without secrets in its records."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ai_platform_support import OWNER, SCOPE, SERVER_IMAGE, SUBNET, owned_fixture  # noqa: F401

from firefly_weave import private_origins as po
from firefly_weave.sdk import platform, platform_ai_setup, platform_docker, platform_operations

WORKER_IMAGE_ID = "sha256:" + "1" * 64
RUNNER_IMAGE_ID = "sha256:" + "2" * 64
PUSHED = "sha256:" + "3" * 64
SUBJECTS = {"weave-operations-worker": str(uuid4()), "weave-operations-runner": str(uuid4())}
HOST = str(uuid4())
MANIFEST = json.loads((Path(__file__).resolve().parents[2] / "fixtures/operations-worker/manifest.json").read_text())


class FakeAPI:
    """The public operations the step calls, each answered once per distinct request and recorded."""

    def __init__(self):
        self.calls = []
        self.fail = None

    async def invoke(self, operation, **kwargs):
        if self.fail == operation:
            self.fail = None
            raise platform.PlatformError("refused once")
        self.calls.append((operation, kwargs))
        if operation == "deployment_runners.list":
            fresh = datetime.now(UTC) + timedelta(seconds=60)
            return SimpleNamespace(items=[SimpleNamespace(revoked=False, expires_at=fresh)])
        return SimpleNamespace(id=uuid4())

    def count(self, operation):
        return sum(1 for name, _ in self.calls if name == operation)


@pytest.fixture
def operations(owned, monkeypatch):
    directory, state, runner = owned
    receipt = {
        "status": "succeeded",
        "output": {"message": "Hello, Weave"},
        "version_id": str(uuid4()),
        "activation_id": str(uuid4()),
        "run_id": str(uuid4()),
        "scope": SCOPE,
    }
    platform._write(directory / "first-run.json", receipt)
    platform._write(directory / "bootstrap.json", {"principal_id": HOST})
    context = directory / "release" / "images"
    context.mkdir(parents=True)
    (context / "operations-worker.py").write_text("# fixture\n")

    def build(command):
        path = Path(command[command.index("--iidfile") + 1])
        path.write_text(RUNNER_IMAGE_ID if "runner" in command else WORKER_IMAGE_ID)
        return b""

    def inspect(command):
        port = json.loads((directory / "operations.json").read_text())["registry_port"]
        registry = f"127.0.0.1:{port}/example-sleep@{PUSHED}"
        return json.dumps([{"Id": WORKER_IMAGE_ID, "RepoDigests": [registry]}]).encode()

    def network(command):
        return json.dumps(
            [
                {
                    "Name": command[-1],
                    "Labels": {"com.docker.compose.project": "weave-local-" + OWNER},
                    "IPAM": {"Config": [{"Subnet": SUBNET}]},
                }
            ]
        ).encode()

    runner.answers.update(
        {
            "operations-runner-build": build,
            "operations-worker-build": build,
            "operations-worker-inspect": inspect,
            "operations-manifest": json.dumps(MANIFEST).encode(),
            "operations-socket": b"991\n",
            "ai-network": network,
            "operations-status": b"running\n",
        }
    )
    api = FakeAPI()

    async def call(state, value, work, transport):
        return await work(api)

    async def clients(state, found, transport):
        return dict(SUBJECTS)

    async def scope(*args):
        return False

    monkeypatch.setattr(platform_operations, "_api", call)
    monkeypatch.setattr(platform_operations, "_keycloak_clients", clients)
    monkeypatch.setattr(platform_ai_setup, "ensure_worker_scope", scope)
    return SimpleNamespace(directory=directory, runner=runner, api=api)


def enable(case):
    return platform_operations.enable(case.directory, platform._load(case.directory))


def test_operations_enable_is_resumable_stage_by_stage(operations):
    case = operations
    stages = {
        "registry": ("runner", "operations-registry"),
        "images": ("runner", "operations-worker-push"),
        "identities": ("api", "principals.link"),
        "release": ("api", "releases.create"),
        "target": ("api", "deployments.create"),
        "runner": ("runner", "operations-runner"),
    }
    for stage, (where, name) in stages.items():
        if where == "runner":
            original = case.runner.answers.get(name, b"")
            case.runner.answers[name] = platform.PlatformError("failed once")
        else:
            case.api.fail = name
        with pytest.raises(platform.PlatformError, match=f"Operations setup stopped at {stage}"):
            enable(case)
        if where == "runner":
            case.runner.answers[name] = original
    result = enable(case)
    assert result["stage"] == "ready" and result["runner"] == "runner-local-docker"
    # Every record was created once in total, however often the step resumed.
    for operation in ("principals.create", "releases.create", "deployment_targets.create", "deployments.create"):
        expected = 2 if operation == "principals.create" else 1
        assert case.api.count(operation) == expected, operation
    assert case.api.count("members.grant") == 3
    assert case.runner.names().count("operations-runner-build") == 1
    # A rerun changes nothing and creates nothing.
    calls = len(case.api.calls)
    assert enable(case)["target_id"] == result["target_id"]
    assert [name for name, _ in case.api.calls[calls:]] == ["deployment_runners.list"]


def test_the_workers_project_and_runner_service_are_exactly_what_the_receipt_says(operations):
    case = operations
    enable(case)
    value = json.loads((case.directory / "operations.json").read_text())
    workers = json.loads((case.directory / "operations" / "workers" / "compose.json").read_text())
    (service,) = workers["services"].values()
    assert workers["name"] == f"weave-local-{OWNER}-workers"
    assert service["deploy"] == {"replicas": 0} and service["stop_grace_period"] == "240s"
    assert service["networks"] == ["platform"] and "ports" not in service
    assert workers["networks"] == {"platform": {"name": f"weave-local-{OWNER}_default", "external": True}}
    assert service["image"] == f"127.0.0.1:{value['registry_port']}/example-sleep@{PUSHED}"
    assert service["labels"]["firefly.weave.target"] == value["target_id"]
    assert all(volume["read_only"] for volume in service["volumes"])
    assert service["user"] == "65532:65532" and service["cap_drop"] == ["ALL"] and service["read_only"]
    assert service["environment"]["WEAVE_API_URL"] == "http://keycloak:8000"
    compose = json.loads((case.directory / "compose.operations.json").read_text())
    runner = compose["services"]["runner-local-docker"]
    assert runner["network_mode"] == "service:keycloak" and runner["group_add"] == ["991"]
    assert runner["read_only"] and runner["cap_drop"] == ["ALL"] and runner["user"] == "65532:65532"
    assert {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"} in runner["volumes"]
    writable = [item for item in runner["volumes"] if not item.get("read_only")]
    assert [item["target"] for item in writable] == ["/var/run/docker.sock", "/run/weave-operations/state"]
    assert compose["services"]["registry"]["ports"] == [f"127.0.0.1:{value['registry_port']}:5000"]
    configuration = json.loads((case.directory / "operations" / "config" / "runner.json").read_text())
    assert configuration["base_url"] == "http://127.0.0.1:8000"
    assert configuration["destination"]["image_repositories"] == [f"127.0.0.1:{value['registry_port']}/example-sleep"]
    # A file changed on disk is refused, never silently rewritten.
    path = case.directory / "compose.operations.json"
    path.write_text(path.read_text().replace("991", "0"))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform._compose(platform._load(case.directory))


def test_operations_receipt_and_compose_files_hold_no_secret_values(operations):
    case = operations
    enable(case)
    secrets = [path.read_text().strip() for path in (case.directory / "operations" / "secrets").iterdir()]
    assert len(secrets) == 2 and all(len(secret) >= 40 for secret in secrets)
    readable = [
        case.directory / "operations.json",
        case.directory / "compose.operations.json",
        case.directory / "platform.json",
        case.directory / "operations" / "workers" / "compose.json",
        *(case.directory / "operations" / "config").iterdir(),
    ]
    status = json.dumps(platform_operations.status(platform._load(case.directory)))
    for secret in secrets:
        assert all(secret not in path.read_text() for path in readable)
        assert secret not in status and all(secret not in json.dumps(command) for _, command in case.runner.calls)
    for path in (case.directory / "operations" / "secrets").iterdir():
        assert path.stat().st_mode & 0o777 == 0o444


def test_operations_entries_cover_only_the_platform_network_subnet(operations):
    case = operations
    enable(case)
    path = case.directory / "private-origins.json"
    before = path.read_bytes()
    policy = po.parse(before)
    found = {(entry.origin, entry.purpose): entry for entry in policy.entries}
    assert set(found) == {
        ("http://keycloak:8000", "platform-api"),
        ("http://keycloak:8080", "worker-auth"),
        ("http://127.0.0.1:8000", "platform-api"),
        ("http://127.0.0.1:8080", "worker-auth"),
    }
    for (origin, _), entry in found.items():
        expected = ((SUBNET,), "bridge") if "keycloak" in origin else (("127.0.0.1/32",), "loopback")
        assert (entry.networks, entry.credentials) == expected
    enable(case)
    assert path.read_bytes() == before


def test_local_docker_lets_the_local_owner_plan_and_approve(operations):
    case = operations
    enable(case)
    (request,) = [kwargs["body"] for name, kwargs in case.api.calls if name == "deployment_targets.create"]
    assert request.approval_policy.distinct_approver is False
    assert request.capabilities == ["observe", "deploy", "update", "scale_workers", "restart_workers"]
    assert "drain_workers" not in request.capabilities and request.name == "local-docker"
    assert request.boundary == f"weave-local-{OWNER}-workers"


def test_no_operations_skips_the_step_and_status_says_so(owned, monkeypatch):
    directory, _, runner = owned
    called = []
    monkeypatch.setattr(platform_operations, "enable", lambda *args, **kwargs: called.append("enable"))
    monkeypatch.setattr(platform_operations, "prepare_provider", lambda state: called.append("provider"))
    monkeypatch.setattr(platform, "start", lambda directory, progress=None: None)
    monkeypatch.setattr(platform, "demo", lambda directory: {"receipt": {"status": "succeeded"}})
    result = platform.up(directory, Path("."), None, operations=False)
    assert called == [] and "operations" not in result
    assert not any("build" in name for name in runner.names())
    assert platform_operations.status(platform._load(directory)) == {
        "stage": "skipped",
        "runner": "absent",
        "target": "local-docker",
    }


def test_doctor_explains_the_docker_socket():
    (note,) = platform_operations.doctor_notes()
    assert "Docker socket" in note and "local development only" in note


def test_the_provider_accepts_the_operations_clients_once(owned):
    directory, state, _ = owned
    assert platform_operations.prepare_provider(state) is True
    providers = json.loads(platform._env_file(directory / "runtime.env")["WEAVE_OIDC_PROVIDERS"])
    assert providers[0]["clients"] == {
        "weave-cli": "human",
        "weave-operations-worker": "application",
        "weave-operations-runner": "application",
    }
    assert platform_operations.prepare_provider(state) is False
    assert platform_docker.environment(state, {})["WEAVE_OIDC_PROVIDERS"]


def test_the_api_environment_accepts_exactly_the_operations_clients(owned):
    directory, state, _ = owned
    path = directory / "runtime.env"
    text = path.read_text()
    path.write_text(text.replace('"weave-cli": "human"', '"weave-cli": "human", "intruder": "application"'))
    with pytest.raises(platform.PlatformError, match="identity configuration"):
        platform_docker.environment(state, {})
    path.write_text(text)
    platform_operations.prepare_provider(state)
    platform_docker.environment(state, {})


def test_the_host_account_may_register_the_target_before_it_does(operations):
    case = operations
    enable(case)
    names = [name for name, _ in case.api.calls]
    grants = [kwargs["body"] for name, kwargs in case.api.calls if name == "members.grant"]
    host = [grant for grant in grants if str(grant.principal_id) == HOST]
    assert [(grant.role, str(grant.environment_id)) for grant in host] == [
        ("deployment_planner", SCOPE["environment_id"])
    ]
    assert names.index("members.grant", names.index("releases.create") + 1) < names.index("deployment_targets.create")


def test_an_upgraded_cli_rerenders_its_files_while_outside_edits_are_still_refused(operations, monkeypatch):
    case = operations
    enable(case)
    render = platform_operations.runner_document

    def upgraded(state, value):
        document = render(state, value)
        document["services"]["registry"]["healthcheck"]["retries"] = 90
        return document

    # A newer CLI renders the same receipt differently: Compose still accepts the files this CLI wrote, and the
    # next up writes the new ones.
    monkeypatch.setattr(platform_operations, "runner_document", upgraded)
    platform._compose(platform._load(case.directory))
    enable(case)
    written = json.loads((case.directory / "compose.operations.json").read_text())
    assert written["services"]["registry"]["healthcheck"]["retries"] == 90
    platform._compose(platform._load(case.directory))


def test_an_interrupted_render_is_never_taken_for_an_outside_edit(operations, monkeypatch):
    case = operations
    enable(case)
    render = platform_operations.runner_document

    def upgraded(state, value):
        document = render(state, value)
        document["services"]["registry"]["healthcheck"]["retries"] = 90
        return document

    monkeypatch.setattr(platform_operations, "runner_document", upgraded)
    write = platform_operations.files._private

    def interrupted(path, data):
        write(path, data)
        raise KeyboardInterrupt

    # The new file is written, then the command stops before it records what it wrote.
    monkeypatch.setattr(platform_operations.files, "_private", interrupted)
    with pytest.raises(KeyboardInterrupt):
        enable(case)
    monkeypatch.setattr(platform_operations.files, "_private", write)
    platform._compose(platform._load(case.directory))
    enable(case)
    value = json.loads((case.directory / "operations.json").read_text())
    assert "files_pending" not in value
    # An edit after that is still refused.
    path = case.directory / "compose.operations.json"
    path.write_text(path.read_text().replace("90", "91"))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform._compose(platform._load(case.directory))


@pytest.mark.parametrize(
    "name",
    [
        "workers/compose.json",
        "config/runner.json",
        "config/worker-oauth.json",
        "config/runner-oauth.json",
        "docker-config/config.json",
    ],
)
def test_every_generated_file_is_checked_before_platform_commands_use_it(operations, name):
    case = operations
    enable(case)
    value = json.loads((case.directory / "operations.json").read_text())
    assert name in value["files"]
    path = case.directory / "operations" / name
    path.chmod(0o644)
    path.write_text(path.read_text().replace("{", "{ ", 1))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform_operations.verify_files(platform._load(case.directory), value)
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform._compose(platform._load(case.directory))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        enable(case)


def test_up_prepares_the_provider_before_starting_and_sets_operations_up_after(owned, monkeypatch):
    directory, _, _ = owned
    called = []
    monkeypatch.setattr(platform_operations, "prepare_provider", lambda state: called.append("provider"))
    monkeypatch.setattr(
        platform_operations, "enable", lambda *args, **kwargs: called.append("enable") or {"stage": "ready"}
    )
    monkeypatch.setattr(platform, "start", lambda directory, progress=None: called.append("start"))
    monkeypatch.setattr(
        platform, "demo", lambda directory: called.append("demo") or {"receipt": {"status": "succeeded"}}
    )
    platform.up(directory, Path("."), None)
    assert called[:3] == ["provider", "start", "demo"] and "enable" in called[3:]


@pytest.mark.parametrize(("flags", "expected"), [([], True), (["--no-operations"], False)])
def test_the_cli_passes_no_operations_to_up(tmp_path, monkeypatch, flags, expected):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli

    seen = {}

    def up(*args, **kwargs):
        seen.update(kwargs)
        return {"api_url": "http://127.0.0.1:8000"}

    monkeypatch.setattr(platform, "up", up)
    CliRunner().invoke(cli, ["platform", "--directory", str(tmp_path / "platform"), "up", *flags, "--output", "json"])
    assert seen["operations"] is expected


def test_ai_and_operations_share_their_loopback_entries_once():
    from firefly_weave.sdk import platform_origins

    operations_entries = platform_operations.entries({}, SUBNET)
    loopback = [entry for entry in operations_entries if entry.credentials == "loopback"]
    value = {
        "ai": {"entries": platform_origins.entry_records(loopback)},
        "operations": {"entries": platform_origins.entry_records(operations_entries)},
    }
    rendered = json.loads(platform_origins._render(value))
    pairs = [(item["origin"], item["purpose"]) for item in rendered["entries"]]
    assert sorted(pairs) == sorted({(entry.origin, entry.purpose) for entry in operations_entries})


@pytest.mark.parametrize("answer", [b"991x\n", b"-1\n", b"\n", b"9 9\n", "\u00b2\n".encode()])
def test_a_socket_group_that_is_not_a_number_stops_the_runner_setup(owned, answer):
    directory, state, runner = owned
    runner.answers["operations-socket"] = answer
    with pytest.raises(platform.PlatformError, match="socket's group could not be read"):
        platform_operations._socket_group(state, {"runner_image": "weave-operations-runner@" + RUNNER_IMAGE_ID})


def test_a_failed_image_build_leaves_no_image_id_file(operations):
    case = operations

    def broken(command):
        Path(command[command.index("--iidfile") + 1]).write_text(WORKER_IMAGE_ID)
        raise platform.PlatformError("build failed")

    case.runner.answers["operations-worker-build"] = broken
    with pytest.raises(platform.PlatformError):
        enable(case)
    assert list(case.directory.glob("operations-*.id")) == []


def test_stop_lets_the_fixture_workers_finish_before_the_platform_stops(operations, monkeypatch):
    from firefly_weave.sdk import platform_docker

    case = operations
    enable(case)
    seen = []
    monkeypatch.setattr(platform_docker, "stop", lambda state: seen.append(list(case.runner.names())))
    platform.stop(case.directory)
    (before_api,) = seen
    assert "operations-workers-stop" in before_api
    command = next(command for name, command in case.runner.calls if name == "operations-workers-stop")
    assert command[command.index("--project-name") + 1] == f"weave-local-{OWNER}-workers"
    assert command[-3:] == ["stop", "--timeout", "240"]


def test_the_workers_receive_only_their_own_private_origin_entries(operations):
    from firefly_weave.sdk import platform_origins

    case = operations
    enable(case)
    workers = json.loads((case.directory / "operations" / "workers" / "compose.json").read_text())
    (service,) = workers["services"].values()
    (policy,) = [item for item in service["volumes"] if item["target"] == platform_origins.CONTAINER_PATH]
    document = json.loads(Path(policy["source"]).read_text())
    assert document["platform"] == "local-development"
    assert sorted((item["origin"], item["purpose"], item["credentials"]) for item in document["entries"]) == sorted(
        [
            (platform_operations.BRIDGE_API, "platform-api", "bridge"),
            (platform_operations.BRIDGE_KEYCLOAK, "worker-auth", "bridge"),
            (platform_operations.KEYCLOAK_ORIGIN, "worker-auth", "loopback"),
        ]
    )
    value = json.loads((case.directory / "operations.json").read_text())
    assert "config/worker-origins.json" in value["files"]


def test_a_lost_principal_response_is_retried_with_the_same_key(operations):
    """The platform answers a repeated key with the principal it created, so a resumed setup makes no second one."""
    case = operations
    invoke = case.api.invoke
    lost = []

    async def losing(operation, **kwargs):
        result = await invoke(operation, **kwargs)
        if operation == "principals.create" and not lost:
            lost.append(kwargs)
            raise platform.PlatformError("response lost")
        return result

    case.api.invoke = losing
    with pytest.raises(platform.PlatformError, match="Operations setup stopped at identities"):
        enable(case)
    enable(case)
    keys = [kwargs.get("idempotency_key") for name, kwargs in case.api.calls if name == "principals.create"]
    owner = platform._load(case.directory)["id"]
    assert keys == [
        f"weave-platform-operations-{owner}-worker-principal",
        f"weave-platform-operations-{owner}-worker-principal",
        f"weave-platform-operations-{owner}-runner-principal",
    ]


def test_the_runner_keeps_docker_state_on_its_temporary_space(operations):
    """The Docker CLI writes buildx state under its configuration directory, which the runner mounts read-only:
    every Compose dry run then failed. The runner points buildx at its private temporary space instead."""
    case = operations
    enable(case)
    compose = json.loads((case.directory / "compose.operations.json").read_text())
    runner = compose["services"]["runner-local-docker"]
    assert runner["environment"]["BUILDX_CONFIG"].startswith("/tmp/")
    assert any(item.startswith("/tmp:") for item in runner["tmpfs"])
    configuration = [item for item in runner["volumes"] if item["target"].endswith("/docker-config")]
    assert configuration and all(item["read_only"] for item in configuration)
