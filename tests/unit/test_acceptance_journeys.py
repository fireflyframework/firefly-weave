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

"""Step enablement (journeys.toml) and pinned versions (versions.toml) follow the harness rules."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journeys = script("acceptance_journeys")
CAPABILITIES = "\n".join(
    f'"{name}" = {"true" if name == "acceptance-foundations" else "false"}' for name in journeys.CAPABILITIES
)
PROFILES = "\n".join(f'{profile} = ["J0"]' for profile in journeys.PROFILES)
CRITERIA = "\n".join(f'{journey} = ["SC7"]' for journey in journeys.JOURNEYS)


def parse(steps, extra=""):
    text = f"[capabilities]\n{CAPABILITIES}\n[profiles]\n{PROFILES}\n[criteria]\n{CRITERIA}\n[steps]\n{steps}\n{extra}"
    return journeys.Enablement.parse(text)


def test_requirements_need_every_capability_and_alternatives_need_one():
    enablement = parse(
        '"J0.1" = ["acceptance-foundations"]\n'
        '"J0.2" = ["acceptance-foundations", "compose-operations"]\n'
        '"J0.3" = ["ai-models-screen|acceptance-foundations"]\n'
        '"J0.3/owner-roles" = ["ai-models-screen|compose-operations"]'
    )
    assert enablement.missing("J0.1") == ()
    assert enablement.missing("J0.2") == ("compose-operations",)
    assert enablement.missing("J0.3") == ()
    assert enablement.missing("J0.3/owner-roles") == ("ai-models-screen|compose-operations",)
    assert enablement.checks("J0.3") == ("J0.3/owner-roles",)
    assert enablement.journey_steps("J0") == ("J0.1", "J0.2", "J0.3")


def test_a_check_inherits_its_step_requirements_and_profiles():
    enablement = parse(
        '"J0.12" = ["real-platform-journeys"]\n"J0.12/model" = ["acceptance-foundations"]',
        '[step_profiles]\n"J0.12" = ["ai"]',
    )
    assert enablement.missing("J0.12/model") == ("real-platform-journeys",)
    assert enablement.applies("J0.12", "ai") and not enablement.applies("J0.12", "pr")
    assert not enablement.applies("J0.12/model", "pr")


@pytest.mark.parametrize(
    "steps",
    [
        '"J16.1" = ["acceptance-foundations"]',
        '"J0.0" = ["acceptance-foundations"]',
        '"J0.1" = []',
        '"J0.1" = ["teleportation"]',
        '"J0.1" = ["Acceptance-Foundations"]',
        '"J0.1" = ["ai-models-screen|teleportation"]',
        '"J0.1" = ["acceptance-foundations"]\n"J0.1/Owner" = ["acceptance-foundations"]',
        '"J0.3/owner" = ["acceptance-foundations"]',
    ],
)
def test_invalid_steps_are_refused(steps):
    with pytest.raises(journeys.JourneysInvalid):
        parse(steps)


def test_capabilities_profiles_and_criteria_must_be_complete():
    good = '"J0.1" = ["acceptance-foundations"]'
    with pytest.raises(journeys.JourneysInvalid):
        journeys.Enablement.parse(
            f"[capabilities]\n{CAPABILITIES.replace(chr(34) + 'release-verification' + chr(34) + ' = false', '')}\n"
            f"[profiles]\n{PROFILES}\n[criteria]\n{CRITERIA}\n[steps]\n{good}"
        )
    with pytest.raises(journeys.JourneysInvalid):
        parse(good, '[step_profiles]\n"J0.1" = ["weekly"]')
    with pytest.raises(journeys.JourneysInvalid):
        journeys.Enablement.parse(f"[capabilities]\n{CAPABILITIES}\n[profiles]\n{PROFILES}\n[steps]\n{good}")


@pytest.mark.parametrize("unknown", ["teleportation", "Compose-Operations", "compose_operations", "loops-v2"])
def test_unknown_capability_keys_are_refused(unknown):
    good = '"J0.1" = ["acceptance-foundations"]'
    table = f'{CAPABILITIES}\n"{unknown}" = false'
    with pytest.raises(journeys.JourneysInvalid, match=f"unknown capabilities: {unknown}"):
        journeys.Enablement.parse(
            f"[capabilities]\n{table}\n[profiles]\n{PROFILES}\n[criteria]\n{CRITERIA}\n[steps]\n{good}"
        )


def test_an_unknown_table_is_refused():
    with pytest.raises(journeys.JourneysInvalid, match="tables capabilities"):
        parse('"J0.1" = ["acceptance-foundations"]', "[features]\nloops = true")


def test_capabilities_are_unique_product_names():
    assert len(set(journeys.CAPABILITIES)) == len(journeys.CAPABILITIES)
    assert [name for name in journeys.CAPABILITIES if journeys.CAPABILITY.fullmatch(name) is None] == []


def test_the_committed_map_is_valid():
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    assert enablement.capabilities["acceptance-foundations"] is True
    assert [name for name, landed in enablement.capabilities.items() if landed] == ["acceptance-foundations"]
    assert enablement.journey_steps("J0") == tuple(f"J0.{n}" for n in range(1, 15))
    assert enablement.profiles["pr"] == ("J0", "J1", "J2", "J3", "J5", "J8", "J9")
    assert enablement.criteria["J3"] == ("SC1", "SC3", "SC7")
    assert enablement.applies("J0.12", "ai") and not enablement.applies("J0.12", "pr")
    assert enablement.missing("J0.3/owner-roles") == ("ai-models-screen|compose-operations",)


def test_versions_toml_pins_match_the_files_that_use_them():
    versions = journeys.load_versions(ROOT / "tests/acceptance/versions.toml")
    images = versions["images"]

    def read(relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    compose = yaml.safe_load(read("compose.yaml"))
    identity = yaml.safe_load(read("compose.identity.yaml"))
    assert compose["services"]["postgres"]["image"] == images["postgres"]
    assert identity["services"]["keycloak-db"]["image"] == images["postgres"]
    assert identity["services"]["keycloak"]["image"] == images["keycloak"]
    assert re.findall(r"^FROM (\S+)", read("Dockerfile"), flags=re.MULTILINE)[:2] == [images["uv"], images["python"]]
    assert re.findall(r"^FROM (\S+)", read("tests/acceptance/fixtures/acme_api/Dockerfile"), flags=re.MULTILINE) == [
        images["python"]
    ]
    package = json.loads(read("studio/package.json"))
    assert package["devDependencies"]["@playwright/test"] == versions["tools"]["playwright"]
    ci = read(".github/workflows/ci.yml")
    assert f"node-version: '{versions['tools']['node']}'" in ci
    assert f"version: '{versions['tools']['uv']}'" in ci


def test_versions_toml_refuses_unpinned_images(tmp_path):
    path = tmp_path / "versions.toml"
    path.write_text('version = 1\n[tools]\nnode = "24.15.0"\n[images]\npostgres = "postgres:17-alpine"\n')
    with pytest.raises(journeys.JourneysInvalid):
        journeys.load_versions(path)


def declared_in_tests():
    found = set()
    for path in sorted((ROOT / "tests/acceptance").glob("test_*.py")):
        content = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'journey_step\("([^"]+)"\)', content))
        found |= set(re.findall(r'\.check\(\s*"([^"]+)"', content))
    specs = ROOT / "studio/tests/acceptance"
    for path in sorted(specs.rglob("*.spec.ts")) if specs.is_dir() else []:
        found |= set(re.findall(r'\bstep\(\s*"(J[^"]+)"', path.read_text(encoding="utf-8")))
    return found


def test_every_enabled_step_has_a_test_and_every_tested_step_is_declared():
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    tested = declared_in_tests()
    enabled = {key for key in enablement.steps if not enablement.missing(key)}
    assert sorted(enabled - tested) == []
    assert sorted(tested - set(enablement.steps)) == []
