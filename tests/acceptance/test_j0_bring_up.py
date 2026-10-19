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

"""J0: clean-machine bring-up through the CLI.

Runs only through scripts/acceptance.py (the up stage). Each test is one journeys.toml
step; the checks that later capabilities add run inside their step through run.check(...).
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from uuid import UUID

import pytest

from firefly_weave import __version__
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.sdk.platform import LOCAL_OWNER_ROLES, PERSON_ROLES

pytestmark = pytest.mark.acceptance

# The acceptance people with the roles that exist at this commit; the roles that later
# capabilities add (step_tester, deployment_log_reader, alert_manager) are checks of J0.4.
PEOPLE = {
    "builder": ("developer", "deployer", "operator", "viewer", "tenant_admin", "lumi_user", "task_participant"),
    "approver": ("task_participant", "task_manager", "deployment_approver"),
    "operator": (
        "operator",
        "viewer",
        "worker_operator",
        "deployment_reader",
        "deployment_planner",
        "deployment_approver",
        "deployment_operator",
    ),
    "viewer": ("viewer",),
}
SECRET_HANDLES = ("acme-api-key", "order-review-webhook")
ACME_ALIAS = "acme.acceptance.test"


def ready(api_url: str, timeout: float = 60) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(api_url + "/health/ready", timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def _expect_roles(grants: list[dict[str, object]], roles: tuple[str, ...]) -> None:
    assert sorted({str(grant["role"]) for grant in grants}) == sorted(roles)


@pytest.mark.journey_step("J0.1")
def test_j0_01_version(run):
    assert run.require(run.weave("--version")).stdout == f"Firefly Weave {__version__}\n"


@pytest.mark.journey_step("J0.2")
def test_j0_02_doctor(run):
    result = run.platform("doctor", "--source", str(run.source), "--context", run.context, "--output", "json")
    report = json.loads(run.require(result).stdout)
    assert report["ok"] is True and report["version"] == __version__ and report["context"] == run.context


@pytest.mark.journey_step("J0.3")
def test_j0_03_up_with_the_fixture_origin(run):
    result = run.require(run.platform(*run.up_arguments(), "--username", "owner", "--output", "json", timeout=1500))
    value = json.loads(result.stdout)
    # Saved outside an assert: pytest would print the call's arguments, the password among them.
    saved_owner = run.save_person("owner", value["account"])
    assert saved_owner.stat().st_mode & 0o777 == 0o600
    assert value["mode"] == "docker" and ready(value["api_url"])
    assert value["private_origins"]["label"] == "Development only"
    saved = run.platform_state()["private_origins"]
    inspected = json.loads(run.require(run.docker("network", "inspect", saved["network"])).stdout)[0]
    assert [config["Subnet"] for config in inspected["IPAM"]["Config"]] == [saved["subnet"]]
    document = json.loads((run.platform_dir / "private-origins.json").read_text(encoding="utf-8"))
    assert document == {
        "format": "weave/private-origins-v1",
        "platform": "local-development",
        "entries": [
            {"origin": origin, "purpose": purpose, "networks": [saved["subnet"]], "credentials": "bridge"}
            for origin in sorted(run.origins)
            for purpose in ("event-delivery", "http-connector")
        ],
    }
    run.check(
        "J0.3/owner-roles",
        lambda: _expect_roles(value["account"]["grants"], LOCAL_OWNER_ROLES),
    )


@pytest.mark.journey_step("J0.4")
def test_j0_04_people(run):
    for name, roles in PEOPLE.items():
        granted = [role for role in roles if role in PERSON_ROLES]
        arguments = ["user", "--username", name]
        for role in granted:
            arguments += ["--role", role]
        value = json.loads(run.require(run.platform(*arguments, "--output", "json")).stdout)
        run.save_person(name, value)
        assert sorted({grant["role"] for grant in value["grants"]}) == sorted(granted)
    for name in ("owner", *PEOPLE):
        assert len(run.keycloak_users(name)) == 1, name


@pytest.mark.journey_step("J0.5")
async def test_j0_05_approvers_group_bound_in_the_demo_environment(run):
    group_request = OPERATIONS["human_groups.put"].request
    binding_request = OPERATIONS["human_assignments.put"].request
    async with run.client("approver") as approver:
        identity = await approver.invoke("identity.read")
        group = await approver.invoke(
            "human_groups.put",
            body=group_request.model_validate_json(
                json.dumps({"name": "approvers", "member_ids": [str(identity.principal_id)]})
            ),
            idempotency_key=f"j0-05-group-{run.run_id}",
        )
        binding = await approver.invoke(
            "human_assignments.put",
            body=binding_request.model_validate_json(json.dumps({"name": "approvers", "group_ids": [str(group.id)]})),
            idempotency_key=f"j0-05-binding-{run.run_id}",
        )
        listed = await approver.invoke("human_assignments.list")
    assert group.name == "approvers" and group.member_ids == [identity.principal_id]
    found = [
        (item.name, item.group_ids, item.enabled) for item in listed.items if item.binding_id == binding.binding_id
    ]
    assert found == [("approvers", [group.id], True)]
    run.save_private("human", {"group_id": str(group.id), "binding_id": str(binding.binding_id)})


@pytest.mark.journey_step("J0.6")
def test_j0_06_integrations(run):
    value = json.loads(run.require(run.platform("integrations", "enable", "--output", "json")).stdout)
    if value["restart_required"]:
        run.restart()
    status = run.status()
    assert status["integrations_enabled"] is True and status["api_ready"] is True


@pytest.mark.journey_step("J0.7")
def test_j0_07_secret_handles(run):
    for handle in SECRET_HANDLES:
        value = run.canary(handle)
        arguments = ("secret", "set", "--handle", handle, "--value-stdin", "--output", "json")
        result = run.require(run.platform(*arguments, input=value))
        if value in result.stdout + result.stderr:
            # Never through assert: pytest would print both operands, the value among them.
            pytest.fail(f"weave platform secret set printed the value of {handle}")
    run.restart()
    listed = json.loads(run.require(run.platform("secret", "list", "--output", "json")).stdout)
    assert set(SECRET_HANDLES) <= set(listed["handles"])
    lines = (run.platform_dir / "api-container.env").read_text(encoding="utf-8").splitlines()
    environment = dict(line.split("=", 1) for line in lines if "=" in line)
    assert set(SECRET_HANDLES) <= {grant["handle"] for grant in json.loads(environment["WEAVE_SECRET_GRANTS"])}
    assert run.status()["api_ready"] is True


@pytest.mark.journey_step("J0.8")
def test_j0_08_fixtures_on_the_egress_network(run):
    run.require(run.fixtures("up", "--detach", "--wait", "--wait-timeout", "120", "--no-build", "acme-api"))
    network = run.platform_state()["private_origins"]["network"]
    container = run.require(run.fixtures("ps", "--quiet", "acme-api")).stdout.strip()
    networks = json.loads(
        run.require(run.docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container)).stdout
    )
    assert list(networks) == [network]
    assert ACME_ALIAS in (networks[network].get("Aliases") or [])
    assert run.acme_control("/_control/journal") == {"entries": []}
    run.check("J0.8/catalog", lambda: asyncio.run(_publish_catalog(run)))
    run.check("J0.8/child-workflow", lambda: asyncio.run(_publish_child_workflow(run)))


GRANTED = ["weave-connector-http-read@2.0.0", "weave-connector-http-write@2.0.0"]


async def _publish_catalog(run) -> None:
    """As the owner: connection acme-local, the five acme-* actions and risk-tier, then the release grant."""
    from firefly_weave.contracts.catalog import PublicationRequest

    catalog = run.helper("acceptance_catalog")
    async with run.client("owner") as owner:
        version = await catalog.connector_version(owner)
        connection = await owner.invoke("connections.create", body=catalog.connection_request(version))
        actions = {}
        for name, document in catalog.actions().items():
            published = await owner.invoke(
                "definitions.publish",
                collection="actions",
                body=PublicationRequest(source=json.dumps(document), format="json"),
                idempotency_key=f"j0-08-{name}-{run.run_id}",
            )
            actions[name] = str(published.id)
        table = await owner.invoke(
            "definitions.publish",
            collection="decision-tables",
            body=PublicationRequest(source=catalog.decision_table(), format="yaml"),
            idempotency_key=f"j0-08-risk-tier-{run.run_id}",
        )
        listed = await owner.invoke("definitions.list", collection="actions", query={"limit": 100})
        tables = await owner.invoke("definitions.list", collection="decision-tables", query={"limit": 100})
    assert set(catalog.ACTION_NAMES) <= {getattr(item, "name", None) for item in listed.items}
    assert ("risk-tier", "1.0.0") in {
        (getattr(item, "name", None), getattr(item, "version", None)) for item in tables.items
    }
    granted = run.require(
        run.platform(
            "integrations",
            "grant",
            "--connection",
            str(connection.id),
            "--access",
            "read",
            "--access",
            "write",
            "--output",
            "json",
        )
    )
    assert sorted(json.loads(granted.stdout)["capabilities"]) == GRANTED
    run.save_private(
        "catalog",
        {"connection_revision_id": str(connection.id), "actions": actions, "decision_table_id": str(table.id)},
    )


async def _publish_child_workflow(run) -> None:
    """As the owner: the callable child workflow notify-customer 1.0.0."""
    catalog = run.helper("acceptance_catalog")
    async with run.client("owner") as owner:
        published = await owner.publish(
            "workflows", catalog.child_workflow(), "yaml", idempotency_key=f"j0-08-child-{run.run_id}"
        )
        listed = await owner.list_definitions("workflows", limit=100)
    assert (published.kind, published.name, published.version) == ("Workflow", catalog.CHILD_WORKFLOW, "1.0.0")
    assert published.id in {getattr(item, "id", None) for item in listed.items}
    run.save_private("child-workflow", {"version_id": str(published.id), "artifact_digest": published.digest})


@pytest.mark.journey_step("J0.9")
async def test_j0_09_the_child_workflow_is_active_and_callable_in_the_demo_environment(run):
    from firefly_weave.contracts.catalog import ActivationRequest

    child = run.private_record("child-workflow")
    async with run.client("owner") as owner:
        request = ActivationRequest(
            version_id=UUID(child["version_id"]), artifact_digest=child["artifact_digest"], scope=owner.scope
        )
        activation = await owner.activate(request, idempotency_key=f"j0-09-child-{run.run_id}")
        read = await owner.read_activation(activation.id)
        exported = await owner.export_definition("workflows", request.version_id)
    name = run.helper("acceptance_catalog").CHILD_WORKFLOW
    assert (read.name, read.retired, read.request.version_id) == (name, False, request.version_id)
    assert read.request.scope == owner.scope and read.workflow_call_pins == []
    assert "callable" in exported.document["spec"] and exported.retired is False
    run.save_private("child-workflow", {**child, "activation_id": str(activation.id)})


@pytest.mark.journey_step("J0.14")
def test_j0_14_status_and_image_inventory(run):
    status = run.status()
    assert status["api_ready"] and status["identity_ready"] and status["integrations_enabled"]
    (run.evidence / "status-j0.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    running = run.project_images()
    # Content keys, not image IDs: under the containerd image store a cache-hit rebuild gets a new ID.
    inventory = {item["content"] for item in run.config["inventory"]}
    assert running, "no container of this run is running"
    assert running <= inventory, f"image content built or pulled after prepare: {sorted(running - inventory)}"
