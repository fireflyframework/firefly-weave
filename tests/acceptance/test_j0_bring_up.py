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

"""J0: clean-machine bring-up through the CLI (acceptance spec §2.4).

Runs only through scripts/acceptance.py (the up stage). Each test is one journeys.toml
step; the checks later milestones add run inside their step through run.check(...).
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from firefly_weave import __version__
from firefly_weave.sdk.platform import PERSON_ROLES

pytestmark = pytest.mark.acceptance

# People of acceptance spec §2.2 with the roles that exist at this commit; the roles later
# milestones add (step_tester, deployment_log_reader, alert_manager) are checks of J0.4.
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
    assert run.save_person("owner", value["account"]).stat().st_mode & 0o777 == 0o600
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
        assert value not in result.stdout + result.stderr
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
