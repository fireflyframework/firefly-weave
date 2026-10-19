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

import pytest

from firefly_weave import __version__
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
    async with run.client("approver") as approver:
        principal = str((await approver.invoke("identity.read")).principal_id)
    members = {"name": "approvers", "member_ids": [principal]}
    async with run.cli("approver") as weave:
        group = weave("human-groups", "put", "--idempotency-key", f"j0-05-group-{run.run_id}", request=members)
        binding = weave(
            "human-assignments",
            "put",
            "--idempotency-key",
            f"j0-05-binding-{run.run_id}",
            request={"name": "approvers", "group_ids": [group["id"]]},
        )
        listed = weave("human-assignments", "list")
        # Read back: no operation lists groups, but the server holds this one at revision 1, so creating it
        # again (expected revision 0) is refused.
        again = weave.refused("human-groups", "put", "--idempotency-key", f"j0-05-again-{run.run_id}", request=members)
    assert (group["name"], group["member_ids"], group["revision"]) == ("approvers", [principal], 1)
    assert (again["status"], again["code"]) == (409, "WV-GROUP-REVISION")
    found = [
        (item["name"], item["group_ids"], item["enabled"])
        for item in listed["items"]
        if item["binding_id"] == binding["binding_id"]
    ]
    assert found == [("approvers", [group["id"]], True)]
    run.save_private("human", {"group_id": group["id"], "binding_id": binding["binding_id"]})


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
    """As the owner, through the CLI: connection acme-local, the five acme-* actions and risk-tier, then the grant."""
    catalog = run.helper("acceptance_catalog")
    async with run.client("owner") as owner:
        version = await catalog.connector_version(owner)
    async with run.cli("owner") as weave:
        connection = weave(
            "connections",
            "create",
            request=json.loads(catalog.connection_request(version).model_dump_json(by_alias=True)),
        )
        actions = {}
        for name, document in catalog.actions().items():
            published = weave(
                *("definitions", "publish", "--collection", "actions"),
                *("--idempotency-key", f"j0-08-{name}-{run.run_id}"),
                request={"source": json.dumps(document), "format": "json"},
            )
            actions[name] = published["id"]
        table = weave(
            *("definitions", "publish", "--collection", "decision-tables"),
            *("--idempotency-key", f"j0-08-risk-tier-{run.run_id}"),
            request={"source": catalog.decision_table(), "format": "yaml"},
        )
        listed = weave("definitions", "list", "--collection", "actions", "--limit", "100")
        tables = weave("definitions", "list", "--collection", "decision-tables", "--limit", "100")
    assert set(catalog.ACTION_NAMES) <= {item.get("name") for item in listed["items"]}
    assert ("risk-tier", "1.0.0") in {(item.get("name"), item.get("version")) for item in tables["items"]}
    granted = run.require(
        run.platform(
            "integrations",
            "grant",
            "--connection",
            connection["id"],
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
        {"connection_revision_id": connection["id"], "actions": actions, "decision_table_id": table["id"]},
    )


async def _publish_child_workflow(run) -> None:
    """As the owner, through the CLI: the callable child workflow notify-customer 1.0.0."""
    catalog = run.helper("acceptance_catalog")
    async with run.cli("owner") as weave:
        published = weave(
            *("definitions", "publish", "--collection", "workflows"),
            *("--idempotency-key", f"j0-08-child-{run.run_id}"),
            request={"source": catalog.child_workflow(), "format": "yaml"},
        )
        listed = weave("definitions", "list", "--collection", "workflows", "--limit", "100")
    assert (published["kind"], published["name"], published["version"]) == ("Workflow", catalog.CHILD_WORKFLOW, "1.0.0")
    assert published["id"] in {item.get("id") for item in listed["items"]}
    run.save_private("child-workflow", {"version_id": published["id"], "artifact_digest": published["digest"]})


@pytest.mark.journey_step("J0.9")
async def test_j0_09_the_child_workflow_is_active_and_callable_in_the_demo_environment(run):
    catalog = run.helper("acceptance_catalog")
    child = run.private_record("child-workflow")
    async with run.cli("owner") as weave:
        activation = weave(
            *("definitions", "activations", "create", "--idempotency-key", f"j0-09-child-{run.run_id}"),
            request={
                "version_id": child["version_id"],
                "artifact_digest": child["artifact_digest"],
                "scope": weave.scope,
            },
        )
        read = weave("definitions", "activations", "read", activation["id"])
        exported = weave("definitions", "export", "--collection", "workflows", child["version_id"])
    assert (read["name"], read.get("retired", False)) == (catalog.CHILD_WORKFLOW, False)
    scope = {key: read["request"]["scope"].get(key) for key in weave.scope}
    assert read["request"]["version_id"] == child["version_id"] and scope == weave.scope
    assert not read.get("workflow_call_pins")
    assert "callable" in exported["document"]["spec"] and exported.get("retired", False) is False
    run.save_private("child-workflow", {**child, "activation_id": activation["id"]})


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
