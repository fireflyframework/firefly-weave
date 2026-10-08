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

"""The acceptance orchestrator's stages, budgets, network choices and records (no Docker needed)."""

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = "20261007t120000-abcdef"


def script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


acceptance = script("acceptance", "weave_acceptance")


def test_stage_budgets_follow_the_spec():
    assert acceptance.stage_minutes("pr") == {"prepare": 30, "up": 25, "run": 35, "collect": 5, "scan": 5, "down": 10}
    assert sum(acceptance.stage_minutes("pr").values()) == 110
    assert set(acceptance.RUN_MINUTES) == set(acceptance.PROFILES)


def test_subnets_leave_room_for_the_egress_network():
    assert acceptance.choose_subnet([]) == "10.231.0.0/24"
    assert acceptance.choose_subnet(["10.231.1.0/24"]) == "10.231.2.0/24"
    assert acceptance.choose_subnet(["172.17.0.0/16", "fd00::/64"]) == "10.231.0.0/24"
    with pytest.raises(acceptance.StageFailed):
        acceptance.choose_subnet(["10.231.0.0/16"])


def test_a_requested_subnet_is_kept_when_it_and_its_egress_network_are_free():
    assert acceptance.choose_subnet([], "10.246.13.0/24") == "10.246.13.0/24"
    # Neighbors that leave both /24 blocks alone, IPv6 networks and larger unrelated blocks do not matter.
    used = ["10.246.12.0/24", "10.246.15.0/24", "172.17.0.0/16", "fd00::/64"]
    assert acceptance.choose_subnet(used, "10.246.13.0/24") == "10.246.13.0/24"


@pytest.mark.parametrize("used", [["10.246.13.0/24"], ["10.246.14.0/24"], ["10.246.0.0/16"], ["10.246.13.128/25"]])
def test_a_requested_subnet_is_refused_when_it_or_its_egress_network_is_in_use(used):
    with pytest.raises(acceptance.StageFailed, match=r"10\.246\.13\.0/24.*10\.246\.14\.0/24.*--subnet"):
        acceptance.choose_subnet(used, "10.246.13.0/24")


def test_egress_rules_return_private_traffic_and_reject_the_rest():
    rules = acceptance.egress_rules()
    prefix = ["sudo", "-n", "iptables", "-w"]
    assert rules[0] == [*prefix, "-N", "WEAVE-ACCEPTANCE"]
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        assert [*prefix, "-A", "WEAVE-ACCEPTANCE", "-d", network, "-j", "RETURN"] in rules
    assert rules[-2] == [*prefix, "-A", "WEAVE-ACCEPTANCE", "-j", "REJECT"]
    assert rules[-1] == [*prefix, "-I", "DOCKER-USER", "1", "-j", "WEAVE-ACCEPTANCE"]
    assert [rule[4] for rule in acceptance.egress_release_rules()] == ["-D", "-F", "-X"]


def test_commands_are_recorded_with_exit_codes_and_the_stage_deadline():
    recorder = acceptance.Recorder(deadline=time.monotonic() + 30)
    result = recorder.run([sys.executable, "-c", "print('hello')"])
    assert result.stdout == "hello\n"
    assert recorder.commands[0]["argv"] == [sys.executable, "-c", "print('hello')"]
    assert recorder.commands[0]["exit"] == 0
    with pytest.raises(acceptance.StageFailed):
        recorder.run([sys.executable, "-c", "raise SystemExit(3)"])
    assert recorder.commands[-1]["exit"] == 3
    with pytest.raises(acceptance.StageFailed, match="ran out of time"):
        acceptance.Recorder(deadline=time.monotonic() - 1).run([sys.executable, "-c", "pass"])


def test_a_failed_stage_skips_run_but_still_collects_scans_and_cleans_up(tmp_path):
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", False, False, {"profile": "pr"})
    order = []

    def stage(name):
        def body(plan, recorder):
            order.append(name)
            if name == "up":
                raise acceptance.StageFailed("up broke")

        return body

    records = acceptance.run_stages(plan, acceptance.STAGES, {name: stage(name) for name in acceptance.STAGES})
    assert order == ["prepare", "up", "collect", "scan", "down"]
    assert [(r["name"], r["exit"], r.get("skipped", False)) for r in records] == [
        ("prepare", 0, False),
        ("up", 1, False),
        ("run", -1, True),
        ("collect", 0, False),
        ("scan", 0, False),
        ("down", 0, False),
    ]
    assert json.loads((tmp_path / "stages.json").read_text()) == records
    assert all(record["argv"][:2] == ["python", "scripts/acceptance.py"] for record in records)


def test_the_summary_validates_even_when_a_stage_failed(tmp_path):
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", False, False, {"profile": "pr"})
    plan.evidence.mkdir()
    records = [{"name": "prepare", "argv": plan.stage_argv("prepare"), "exit": 1, "seconds": 2.0, "commands": []}]
    summary = acceptance.summarize(plan, records)
    assert summary["complete"] is False and summary["profile"] == "pr"
    assert summary["journeys"][0]["id"] == "J0" and summary["environment"]["egress_blocked"] is False


def test_image_content_keys_layers_and_config_not_the_image_id():
    layers = '["sha256:' + "1" * 64 + '"]'
    config = '{"Cmd":["weave","serve"],"User":"10001"}'
    key = acceptance.image_content(layers + config + "\n")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", key)
    assert key == acceptance.image_content(layers + config)
    assert key != acceptance.image_content(layers + config.replace("10001", "0"))
    assert acceptance.IMAGE_CONTENT_FORMAT == "{{json .RootFS.Layers}}{{json .Config}}"


def test_arguments_and_reproduction_commands():
    args = acceptance.parse_args(["--profile", "pr", "--stages", "collect,down", "--run-id", RUN])
    assert args.stages == ("collect", "down") and args.run_id == RUN
    for bad in (["--profile", "pr", "--stages", "deploy"], ["--profile", "pr", "--run-id", "../escape"]):
        with pytest.raises(SystemExit):
            acceptance.parse_args(bad)
    plan = acceptance.Plan(Path("/runs") / RUN, RUN, "pr", "default", True, False)
    assert plan.stage_argv("up") == [
        "python",
        "scripts/acceptance.py",
        "--profile",
        "pr",
        "--run-id",
        RUN,
        "--docker-context",
        "default",
        "--stages",
        "up",
        "--block-egress",
    ]
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{5,62}", acceptance.new_run_id())


def test_the_subnet_argument_takes_a_private_ipv4_slash_24_with_a_private_egress_network():
    assert acceptance.parse_args(["--profile", "pr"]).subnet is None
    assert acceptance.parse_args(["--profile", "pr", "--subnet", "10.246.13.0/24"]).subnet == "10.246.13.0/24"
    assert acceptance.parse_args(["--profile", "pr", "--subnet", "172.30.1.0/24"]).subnet == "172.30.1.0/24"
    assert acceptance.parse_args(["--profile", "pr", "--subnet", "192.168.7.0/24"]).subnet == "192.168.7.0/24"
    bad = (
        "10.246.0.0/16",  # not a /24
        "10.0.0.0/8",
        "10.246.13.0/25",
        "10.246.13.0",  # no prefix length
        "10.246.13.5/24",  # host bits set
        "8.8.8.0/24",  # not RFC 1918
        "172.32.0.0/24",  # just outside 172.16.0.0/12
        "9.255.255.0/24",  # the egress /24 that follows is private, the platform /24 is not
        "172.15.255.0/24",
        "192.167.255.0/24",
        "100.64.0.0/24",  # CGNAT
        "fd00:1::/64",  # IPv6
        "fd00::/24",
        "10.255.255.0/24",  # the egress /24 that follows would leave 10.0.0.0/8
        "172.31.255.0/24",
        "192.168.255.0/24",
        "nonsense",
        "",
    )
    for subnet in bad:
        with pytest.raises(SystemExit):
            acceptance.parse_args(["--profile", "pr", "--subnet", subnet])


def test_reproduction_commands_carry_a_requested_subnet():
    plan = acceptance.Plan(Path("/runs") / RUN, RUN, "pr", "default", True, False, subnet="10.246.13.0/24")
    assert plan.stage_argv("prepare") == [
        "python",
        "scripts/acceptance.py",
        "--profile",
        "pr",
        "--run-id",
        RUN,
        "--docker-context",
        "default",
        "--stages",
        "prepare",
        "--subnet",
        "10.246.13.0/24",
        "--block-egress",
    ]
    assert "--subnet" not in acceptance.Plan(Path("/runs") / RUN, RUN, "pr", "default", False, False).stage_argv("up")


def test_a_resumed_run_refuses_a_different_subnet(tmp_path, capsys):
    run = tmp_path / RUN
    run.mkdir()
    (run / "run.json").write_text(
        json.dumps({"profile": "pr", "context": "default", "subnet": "10.246.13.0/24"}), encoding="utf-8"
    )
    argv = ["--profile", "pr", "--root", str(tmp_path), "--run-id", RUN, "--docker-context", "default"]
    assert acceptance.main([*argv, "--stages", "collect", "--subnet", "10.246.15.0/24"]) == 2
    assert "run's own --subnet" in capsys.readouterr().err
    assert not (run / "stages.json").exists()


class FakeRecorder:
    """Stands in for Recorder in `down`: Docker listings come back empty and iptables commands succeed
    unless their action (-D, -F, -X) is in ``failing`` (non-zero exit) or ``raising`` (StageFailed)."""

    def __init__(self, failing=(), raising=()):
        self.failing, self.raising, self.iptables = set(failing), set(raising), []

    def run(self, argv, **kwargs):
        code = 0
        if list(argv[:3]) == ["sudo", "-n", "iptables"]:
            self.iptables.append(list(argv))
            if argv[4] in self.raising:
                raise acceptance.StageFailed("sudo timed out")
            code = 1 if argv[4] in self.failing else 0
        return subprocess.CompletedProcess(list(argv), code, stdout="", stderr="")


def blocked_plan(tmp_path):
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", True, False, {"profile": "pr", "egress_rules": True})
    plan.private.mkdir()
    plan.evidence.mkdir()
    return plan


def every_stage_passed(plan):
    return [
        {"name": name, "argv": plan.stage_argv(name), "exit": 0, "seconds": 1.0, "commands": []}
        for name in acceptance.STAGES
    ]


ENABLEMENT = acceptance.acceptance_journeys.Enablement.load(acceptance.JOURNEYS)


def record_steps(plan, leave_out=()):
    """steps.jsonl as the harness writes it when every applicable pr step ran, except ``leave_out``: an
    enabled step passes, or is partial when one of its checks waits for milestones; a step that waits
    for milestones is skipped with them."""
    lines = []
    for journey in ENABLEMENT.profiles["pr"]:
        for step in ENABLEMENT.journey_steps(journey):
            if not ENABLEMENT.applies(step, "pr") or step in leave_out:
                continue
            missing = ENABLEMENT.missing(step)
            if missing:
                lines.append({"id": step, "status": "skipped", "missing": list(missing)})
                continue
            skipped = [
                {"check": check, "missing": list(ENABLEMENT.missing(check))}
                for check in ENABLEMENT.checks(step)
                if ENABLEMENT.applies(check, "pr") and ENABLEMENT.missing(check)
            ]
            record = {"id": step, "status": "partial" if skipped else "passed", "seconds": 1.0}
            lines.append({**record, "skipped_checks": skipped} if skipped else record)
    (plan.evidence / "steps.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.parametrize("trouble", [{"failing": {"-D"}}, {"failing": {"-X"}}, {"raising": {"-F"}}])
def test_a_failed_egress_release_stays_visible_as_a_leftover(tmp_path, trouble):
    plan = blocked_plan(tmp_path)
    recorder = FakeRecorder(**trouble)
    with pytest.raises(acceptance.StageFailed, match="iptables:WEAVE-ACCEPTANCE"):
        acceptance.down(plan, recorder)
    assert [argv[4] for argv in recorder.iptables] == ["-D", "-F", "-X"]  # one failure never skips the rest
    assert plan.state["egress_rules"] is True
    assert plan.state["resources_left"] == ["iptables:WEAVE-ACCEPTANCE"]
    assert plan.private.is_dir()
    # Even with every stage recorded as passed, the leftover makes the run incomplete and names it.
    summary = acceptance.summarize(plan, every_stage_passed(plan))
    assert summary["complete"] is False
    assert summary["resources_left"] == ["iptables:WEAVE-ACCEPTANCE"]


def test_a_complete_egress_release_leaves_nothing_listed(tmp_path):
    plan = blocked_plan(tmp_path)
    recorder = FakeRecorder()
    acceptance.down(plan, recorder)
    assert [argv[4] for argv in recorder.iptables] == ["-D", "-F", "-X"]
    assert plan.state["egress_rules"] is False
    assert plan.state["resources_left"] == []
    assert not plan.private.exists()
    record_steps(plan)
    summary = acceptance.summarize(plan, every_stage_passed(plan))
    assert summary["complete"] is True and summary["resources_left"] == []


def test_down_without_an_egress_block_runs_no_iptables_command(tmp_path):
    plan = blocked_plan(tmp_path)
    plan.state["egress_rules"] = False
    recorder = FakeRecorder()
    acceptance.down(plan, recorder)
    assert recorder.iptables == [] and plan.state["resources_left"] == []


class PytestRecorder:
    """Stands in for Recorder in the journey stages: every command exits ``code``."""

    def __init__(self, code):
        self.code, self.commands = code, []

    def run(self, argv, **kwargs):
        self.commands.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.code, stdout="", stderr="")


def test_up_fails_when_pytest_collects_no_j0_test(tmp_path):
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", False, False, {"profile": "pr"})
    with pytest.raises(acceptance.StageFailed, match="No journey test was collected"):
        acceptance.up(plan, PytestRecorder(5))
    recorder = PytestRecorder(0)
    acceptance.up(plan, recorder)
    assert acceptance.J0_TESTS in recorder.commands[0]
    with pytest.raises(acceptance.StageFailed, match="Journey steps failed"):
        acceptance.up(plan, PytestRecorder(1))


def test_the_run_stage_still_accepts_journey_files_without_collected_tests(tmp_path):
    # The step records, not pytest's exit code, show whether an enabled step ran (see the summary tests).
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", False, False, {"profile": "pr"})
    acceptance._journeys(plan, PytestRecorder(5), ["tests/acceptance/test_j1_example.py"], allow_empty=True)
    with pytest.raises(acceptance.StageFailed, match="Journey steps failed"):
        acceptance._journeys(plan, PytestRecorder(1), ["tests/acceptance/test_j1_example.py"], allow_empty=True)


def test_an_enabled_step_without_a_record_makes_the_run_incomplete(tmp_path):
    plan = blocked_plan(tmp_path)
    plan.state["resources_left"] = []
    record_steps(plan, leave_out={"J0.8"})
    summary = acceptance.summarize(plan, every_stage_passed(plan))
    first = summary["journeys"][0]
    assert first["id"] == "J0" and first["status"] == "failed"
    assert {"n": 8, "status": "not_run"} in first["steps"]
    assert summary["complete"] is False
    assert acceptance.failed_journeys(summary["journeys"], acceptance.STAGES) == ["J0"]


def test_a_run_whose_applicable_steps_passed_were_partial_or_skipped_is_complete(tmp_path):
    plan = blocked_plan(tmp_path)
    plan.state["resources_left"] = []
    record_steps(plan)
    summary = acceptance.summarize(plan, every_stage_passed(plan))
    statuses = {step["status"] for journey in summary["journeys"] for step in journey["steps"]}
    assert statuses <= {"passed", "partial", "skipped"} and "passed" in statuses
    assert all(journey["status"] != "failed" for journey in summary["journeys"])
    assert summary["complete"] is True


def stub_stages(monkeypatch, up):
    for name in acceptance.STAGES:
        monkeypatch.setitem(acceptance.STAGE_FUNCTIONS, name, up if name == "up" else lambda plan, recorder: None)


def test_main_fails_when_an_enabled_step_has_no_record_even_though_every_stage_passed(tmp_path, monkeypatch, capsys):
    stub_stages(monkeypatch, lambda plan, recorder: record_steps(plan, leave_out={"J0.8"}))
    argv = ["--profile", "pr", "--root", str(tmp_path), "--run-id", RUN, "--docker-context", "default"]
    assert acceptance.main(argv) == 1
    summary = json.loads((tmp_path / RUN / "evidence" / "acceptance.json").read_text(encoding="utf-8"))
    assert all(stage["exit"] == 0 for stage in summary["stages"])
    assert summary["complete"] is False and summary["journeys"][0]["status"] == "failed"
    assert "journey J0 failed or left an enabled step without a record" in capsys.readouterr().out


def test_main_succeeds_when_every_applicable_step_passed_was_partial_or_skipped(tmp_path, monkeypatch, capsys):
    stub_stages(monkeypatch, lambda plan, recorder: record_steps(plan))
    argv = ["--profile", "pr", "--root", str(tmp_path), "--run-id", RUN, "--docker-context", "default"]
    assert acceptance.main(argv) == 0
    summary = json.loads((tmp_path / RUN / "evidence" / "acceptance.json").read_text(encoding="utf-8"))
    assert summary["complete"] is True
    assert "all requested stages passed" in capsys.readouterr().out


def test_main_judges_only_the_journeys_of_the_requested_stages(tmp_path, monkeypatch):
    # J0 runs in up: a prepare-only invocation cannot fail on it, an up invocation can.
    stub_stages(monkeypatch, lambda plan, recorder: None)
    argv = ["--profile", "pr", "--root", str(tmp_path), "--run-id", RUN, "--docker-context", "default"]
    assert acceptance.main([*argv, "--stages", "prepare"]) == 0
    assert acceptance.main([*argv, "--stages", "up"]) == 1
    monkeypatch.setitem(acceptance.STAGE_FUNCTIONS, "up", lambda plan, recorder: record_steps(plan))
    assert acceptance.main([*argv, "--stages", "up"]) == 0
