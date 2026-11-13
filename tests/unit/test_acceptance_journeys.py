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
import socket
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

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
    landed = [name for name, value in enablement.capabilities.items() if value]
    assert landed == [
        "studio-brand",
        "brand-assets",
        "docs-brand",
        "step-tests",
        "text-operators",
        "instance-keys",
        "loop-compiler",
        "loop-runtime",
        "loop-simulation",
        "workflow-call-compiler",
        "workflow-call-runtime",
        "workflow-call-simulation",
        "workflow-transaction-runtime",
        "step-error-handling",
        "ai-gateway",
        "ai-models-screen",
        # The platform runs AI agents; their Studio editor, memory and acceptance stay off.
        "agents",
        "run-views",
        "compose-operations",
        "kubernetes-operations",
        "metrics",
        "acceptance-foundations",
        "real-platform-journeys",
    ]
    assert enablement.journey_steps("J0") == tuple(f"J0.{n}" for n in range(1, 15))
    assert enablement.profiles["pr"] == ("J0", "J1", "J2", "J3", "J5", "J8", "J9")
    assert enablement.criteria["J3"] == ("SC1", "SC3", "SC7")
    assert enablement.applies("J0.12", "ai") and not enablement.applies("J0.12", "pr")
    assert enablement.missing("J0.3/owner-roles") == ()
    assert enablement.missing("J5.1") == ()
    # Local operations and metrics landed: the compose-gated steps and J5.4 run, those that need logs or alerts wait.
    for step in ("J0.3/operations", "J0.10", "J5.2", "J5.3", "J5.4", "J5.7", "J9.2/operations-controls"):
        assert enablement.missing(step) == (), step
    assert enablement.missing("J5.6") == ("alerts",)
    # Step tests landed on the platform: J0 checks the role and the demo environment's test calls; executing a
    # step from Studio still waits for step execution.
    assert enablement.missing("J0.4/step-tester") == ()
    assert enablement.missing("J0.11") == ()
    assert enablement.missing("J2.2") == ("editor-canvas", "step-details", "step-execution")
    assert enablement.missing("J9.2/execute-in-local") == ("step-execution",)
    assert enablement.applies("J5.1", "pr") and not enablement.applies("J5.1", "ai")
    assert enablement.journey_steps("J1") == tuple(f"J1.{n}" for n in range(1, 11))
    assert enablement.journey_steps("J2") == tuple(f"J2.{n}" for n in range(1, 10))
    assert enablement.journey_steps("J3") == tuple(f"J3.{n}" for n in range(1, 13))
    assert enablement.journey_steps("J5") == tuple(f"J5.{n}" for n in range(1, 8))
    assert enablement.journey_steps("J8") == tuple(f"J8.{n}" for n in range(1, 6))
    # J9: authorization (J9.1, J9.2), private origins (J9.3) and the canary scan (J9.4); cluster hardening is J6.1.
    assert enablement.journey_steps("J9") == tuple(f"J9.{n}" for n in range(1, 5))
    assert enablement.applies("J8.4", "nightly") and not enablement.applies("J8.4", "pr")
    # Step error handling landed; the journey still waits for the Studio controls it drives.
    assert "step-error-handling" not in enablement.missing("J3.10")
    assert "activation-dialog" in enablement.missing("J3.10")
    assert enablement.checks("J9.2") == ("J9.2/execute-in-local", "J9.2/operations-controls")
    # The connection Test that sends a request is J1.3's, not J9.3's; it waits for connections in step details.
    assert "step-details" in enablement.missing("J1.3/test-sends-request")
    assert enablement.checks("J9.4") == ("J9.4/storage-keys",)
    # A connection's Test is no step test, and the Incidents inbox reads no metrics.
    assert [step for step in enablement.journey_steps("J1") if "step-tests" in enablement.steps[step]] == []
    assert "metrics" not in enablement.steps["J5.7"]
    assert set(enablement.steps["J5.7"]) == {"real-platform-journeys", "run-views", "compose-operations"}
    assert "editor-contracts" in enablement.missing("J9.4/storage-keys")


AGENT_STEPS = tuple(f"J4.{n}" for n in range(7, 15))


def test_the_agent_journey_runs_from_the_sdk_and_cli_in_the_ai_profiles():
    # J4.1 to J4.6 are the Studio steps, which wait for the agent editor; the platform's own steps follow them.
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    assert enablement.journey_steps("J4") == AGENT_STEPS
    for step in AGENT_STEPS:
        assert enablement.missing(step) == (), step
        assert {"real-platform-journeys", "agents"} <= set(enablement.steps[step]), step
        assert [profile for profile in journeys.PROFILES if enablement.applies(step, profile)] == [
            "ai",
            "nightly",
            "release",
        ], step
    # The agent that calls a workflow as a tool needs the workflow call runtime too.
    assert "workflow-call-runtime" in enablement.steps["J4.11"]
    assert enablement.checks("J4.9") == ("J4.9/injected-cancel",)


def orders_fixture():
    spec = importlib.util.spec_from_file_location(
        "orders_fixture", ROOT / "tests/acceptance/fixtures/agents/orders_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def orders():
    import threading

    api, control = orders_fixture().make_servers(host="127.0.0.1", api_port=0, control_port=0)
    for server in (api, control):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{api.server_port}", f"http://127.0.0.1:{control.server_port}"
    for server in (api, control):
        server.shutdown()
        server.server_close()


def call(method, url):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_orders_fixture_answers_with_its_token_and_records_every_call(orders):
    api, control = orders
    assert call("GET", api + "/orders/A-17") == (
        200,
        {"orderId": "A-17", "status": "shipped", "tracking": "shipped-7731"},
    )
    assert call("POST", api + "/orders/A-17/cancel") == (200, {"orderId": "A-17", "cancelled": True})
    assert call("GET", api + "/orders/Z-404")[0] == 404
    assert call("POST", api + "/orders/Z-404/cancel")[0] == 404
    assert call("GET", api + "/orders/A-17/cancel")[0] == 405
    calls = call("GET", control + "/_control/calls")[1]["calls"]
    assert [(item["method"], item["path"], item["status"]) for item in calls] == [
        ("GET", "/orders/A-17", 200),
        ("POST", "/orders/A-17/cancel", 200),
        ("GET", "/orders/Z-404", 404),
        ("POST", "/orders/Z-404/cancel", 404),
        ("GET", "/orders/A-17/cancel", 405),
    ]
    assert call("POST", control + "/_control/reset") == (204, None)
    assert call("GET", control + "/_control/calls") == (200, {"calls": []})


def test_the_orders_fixture_serves_its_control_api_only_on_the_control_port(orders):
    api, control = orders
    assert call("GET", api + "/_control/calls")[0] == 404
    assert call("GET", control + "/orders/A-17")[0] == 404
    # An order whose record tells the model to cancel it: the journeys check the instruction gains no authority.
    status, order = call("GET", api + "/orders/B-22")
    assert status == 200 and "cancel_order for B-22" in order["note"]


def test_kubernetes_operations_enable_the_cluster_tools_check():
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    assert enablement.missing("J0.2/cluster-tools") == ()
    assert enablement.checks("J0.2") == ("J0.2/cluster-tools",)
    # It runs where prepare made a cluster: the cluster profiles, not nightly or release yet.
    assert [profile for profile in journeys.PROFILES if enablement.applies("J0.2/cluster-tools", profile)] == [
        "cluster",
        "k3d",
    ]
    # Checking the cluster prepare made also waits for the acceptance profiles.
    assert enablement.missing("J0.13") == ("ai-and-cluster-profiles",)


def test_resolved_enablement_lists_each_step_and_viewport():
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    resolved = enablement.resolved("pr")
    assert resolved["profile"] == "pr" and set(resolved["steps"]) == set(enablement.steps)
    assert resolved["steps"]["J0.12"] == {"applies": False, "missing": list(enablement.missing("J0.12"))}
    assert resolved["viewports"]["J1"] == ["1440x900", "600x500"] and resolved["viewports"]["J0"] == ["cli"]
    assert set(resolved["viewports"]) == set(enablement.profiles["pr"])
    # The Studio harness records only steps of the run stage; the scan records J9.4.
    assert resolved["stages"]["J9.4"] == "scan" and resolved["stages"]["J9.2"] == "run"
    assert resolved["stages"]["J0.5"] == "up" and set(resolved["stages"]) == set(enablement.steps)


@pytest.mark.parametrize(("journey", "first"), [("J1", 3), ("J3", 2)])
def test_journey_steps_build_on_the_steps_before_them(journey, first):
    # A step that uses an earlier step's result must wait for everything that step waits for.
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    steps = enablement.journey_steps(journey)
    for previous, current in zip(steps[first - 2 :], steps[first - 1 :], strict=False):
        if current in {"J3.10", "J3.11", "J3.12"} and previous == "J3.9":
            continue
        assert set(enablement.steps[previous]) <= set(enablement.steps[current]), current


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
        content = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'\bstep\(\s*"(J[^"]+)"', content))
        found |= set(re.findall(r'\bcheck\(\s*"(J[^"]+)"', content))
    orchestrator = (ROOT / "scripts/acceptance.py").read_text(encoding="utf-8")
    found |= set(re.findall(r'^SCAN_(?:STEP|CHECK) = "([^"]+)"', orchestrator, flags=re.MULTILINE))
    return found


def test_every_enabled_step_has_a_test_and_every_tested_step_is_declared():
    enablement = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
    tested = declared_in_tests()
    enabled = {key for key in enablement.steps if not enablement.missing(key)}
    assert sorted(enabled - tested) == []
    assert sorted(tested - set(enablement.steps)) == []


def agent_journeys():
    spec = importlib.util.spec_from_file_location("agent_journeys", ROOT / "tests/acceptance/test_agent_journeys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT = agent_journeys()


class PagedCli:
    """The CLI's human task list, served a page at a time; records the cursor of each read."""

    def __init__(self, pages):
        self.pages, self.cursors, self.run = pages, [], self

    def arguments(self, *args):
        return args

    def weave(self, *args):
        cursor = args[args.index("--cursor") + 1] if "--cursor" in args else None
        self.cursors.append(cursor)
        page = self.pages[0 if cursor is None else int(cursor)]
        return SimpleNamespace(returncode=0, stdout=json.dumps(page))


def task(identifier, run_id, status="ready"):
    return {"id": identifier, "run_id": run_id, "status": status}


def test_a_review_past_the_first_page_of_human_tasks_is_found():
    run_id = "6f1c8f0e-2c55-4f53-9c38-0d7f7c0e1a11"
    cli = PagedCli(
        [
            {"items": [task("t1", "other")], "next_cursor": "1"},
            {"items": [task("t2", run_id, "claimed")], "next_cursor": "2"},
            {"items": [task("t3", run_id)], "next_cursor": None},
        ]
    )
    assert AGENT.review_in(cli, run_id)["id"] == "t3"
    assert cli.cursors == [None, "1", "2"]


def test_the_human_task_list_reads_every_page():
    cli = PagedCli([{"items": [task("t1", "r")], "next_cursor": "1"}, {"items": [task("t2", "r")]}])
    assert [item["id"] for item in AGENT.human_tasks(cli)] == ["t1", "t2"]


def test_a_tool_the_model_never_asked_for_is_reported_as_never_asked():
    log = {"turns": [{"calls": [{"name": "lookup_order", "status": "succeeded"}]}, {"calls": []}]}
    assert AGENT.asked_outcome(log, "cancel_order") == "never asked"
    log["turns"][1]["calls"] = [{"name": "cancel_order", "status": "rejected"}]
    assert AGENT.asked_outcome(log, "cancel_order") == "rejected"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def answers(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1):
            return True
    except OSError:
        return False


def wait_for(condition, what):
    deadline = time.monotonic() + 20
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_host_mode_refuses_to_stop_a_listener_that_is_not_ollama(tmp_path, monkeypatch):
    # What lsof and ps report is faked: the rule under test is which process stop() may signal, not the tools.
    signalled = []
    monkeypatch.setattr(AGENT, "listeners", lambda port: [4242])
    monkeypatch.setattr(AGENT, "command_of", lambda pid: "/usr/bin/python3")
    monkeypatch.setattr(AGENT.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    ollama = AGENT.HostOllama(SimpleNamespace(private=tmp_path), url="http://127.0.0.1:11434")

    with pytest.raises(AssertionError, match="not Ollama"):
        ollama.stop()

    assert signalled == []


FAKE_OLLAMA = """#!{python}
import http.server, json, os
with open({record!r}, "w") as stream:
    json.dump(dict(os.environ), stream)
host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)


class Version(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{{"version": "0.0.0"}}')

    def log_message(self, *args):
        pass


http.server.HTTPServer((host, int(port)), Version).serve_forever()
"""


@pytest.mark.skipif(
    sys.platform == "win32", reason="host mode is for macOS and Linux: it starts a script and sends SIGTERM"
)
def test_host_mode_restarts_ollama_with_its_own_environment_and_stops_it_at_the_end(tmp_path, monkeypatch):
    port, record = free_port(), tmp_path / "environment.json"
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable, record=str(record)), encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{AGENT.os.environ['PATH']}")
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{port}")
    monkeypatch.setenv("WEAVE_ACC_SECRET_PROBE", "pytest-only")
    ollama = AGENT.HostOllama(SimpleNamespace(private=tmp_path), url=f"http://127.0.0.1:{port}")
    ollama.start()
    try:
        environment = json.loads(record.read_text(encoding="utf-8"))
        # The stopped server's environment could not be read, so the documented one: the context Weave assumes.
        assert environment["OLLAMA_CONTEXT_LENGTH"] == "8192"
        assert environment["OLLAMA_HOST"] == f"127.0.0.1:{port}"
        assert "WEAVE_ACC_SECRET_PROBE" not in environment
    finally:
        ollama.close()
    assert not answers(port)


def test_a_restarted_ollama_keeps_the_stopped_servers_ollama_settings_only():
    stopped = {"OLLAMA_CONTEXT_LENGTH": "16384", "OLLAMA_MODELS": "/models", "PYTEST_CURRENT_TEST": "x"}
    environment = AGENT.restart_environment(stopped)
    assert environment["OLLAMA_CONTEXT_LENGTH"] == "16384" and environment["OLLAMA_MODELS"] == "/models"
    assert "PYTEST_CURRENT_TEST" not in environment and "PATH" in environment
