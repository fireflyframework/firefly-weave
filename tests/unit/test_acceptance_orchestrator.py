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
import stat
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
    enabled step passes, or is partial when one of its checks waits for capabilities; a step that waits
    for capabilities is skipped with them."""
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
            if acceptance.acceptance_journeys.step_stage(step) == "scan":
                record["source"] = "scan"
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


class CallRecorder:
    """Records every command; commands whose text holds a ``failing`` marker exit 1."""

    def __init__(self, failing=()):
        self.calls, self.failing = [], tuple(failing)

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        code = 1 if any(marker in " ".join(argv) for marker in self.failing) else 0
        return subprocess.CompletedProcess(list(argv), code, stdout="", stderr="")


def journeys_plan(tmp_path):
    state = {"profile": "pr", "origins": [acceptance.FIXTURE_ORIGIN], "acme_control_port": 40001}
    plan = acceptance.Plan(tmp_path, RUN, "pr", "default", False, False, state)
    plan.private.mkdir(parents=True)
    plan.evidence.mkdir()
    return plan


def test_run_drives_legacy_suites_and_the_studio_journeys(tmp_path):
    plan = journeys_plan(tmp_path)
    recorder = CallRecorder()
    acceptance.run(plan, recorder)
    commands = [" ".join(argv) for argv, _ in recorder.calls]
    assert "-m pytest" in commands[0]
    assert any("tests/browser/real-platform.spec.ts" in command for command in commands)
    studio = [(argv, kwargs) for argv, kwargs in recorder.calls if acceptance.STUDIO_JOURNEYS_CONFIG in argv]
    assert len(studio) == 1 and studio[0][0][-2:] == ["--retries", "0"]
    assert studio[0][1]["env"]["WEAVE_ACC_RUN_DIR"] == str(plan.root)
    assert studio[0][1]["cwd"] == acceptance.ROOT / "studio"
    resolved = json.loads((plan.root / "enablement.json").read_text(encoding="utf-8"))
    assert resolved["profile"] == "pr" and "J1.1" in resolved["steps"]


def test_a_failed_suite_still_runs_the_studio_journeys(tmp_path):
    plan = journeys_plan(tmp_path)
    recorder = CallRecorder(failing=("real-platform.spec.ts",))
    with pytest.raises(acceptance.StageFailed, match="the platform-session suite"):
        acceptance.run(plan, recorder)
    assert any(acceptance.STUDIO_JOURNEYS_CONFIG in argv for argv, _ in recorder.calls)


def journeys_map(tmp_path, enabled):
    """The committed map with real-platform-journeys switched on or off."""
    text = (acceptance.ROOT / "tests/acceptance/journeys.toml").read_text(encoding="utf-8")
    text = re.sub(
        r'"real-platform-journeys" = (true|false)', f'"real-platform-journeys" = {str(enabled).lower()}', text
    )
    path = tmp_path / ("on.toml" if enabled else "off.toml")
    path.write_text(text, encoding="utf-8")
    return path


def scan_steps(plan):
    path = plan.evidence / "steps.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_the_scan_records_j9_4_only_when_it_is_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = journeys_plan(tmp_path / "run")
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, False))
    acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan) == []
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, True))
    acceptance.scan(plan, FakeRecorder())
    (record,) = scan_steps(plan)
    # The storage-key check waits for the editor's account-prefixed storage.
    assert (record["id"], record["status"]) == ("J9.4", "partial")
    assert record["skipped_checks"] == [{"check": "J9.4/storage-keys", "missing": ["editor-contracts"]}]


def test_studio_steps_without_storage_dumps_fail_j9_4(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = journeys_plan(tmp_path / "run")
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, True))
    (plan.evidence / "steps.jsonl").write_text(
        json.dumps({"id": "J1.1", "status": "passed", "viewport": "1440x900"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(acceptance.StageFailed, match="No browser storage was captured"):
        acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1]["status"] == "failed"


def test_scan_counts_browser_storage_entries_and_finds_canaries_in_them(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = journeys_plan(tmp_path / "run")
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, True))
    (plan.private / "canaries.json").write_text(json.dumps({"acme-api-key": "wv-canary-x1-unit"}), encoding="utf-8")
    storage = plan.private / "browser-storage"
    storage.mkdir()
    dump = {"local": {"a": "1"}, "session": {"b": "2"}, "indexed_db": []}
    (storage / "J1-1440x900-builder.json").write_text(json.dumps(dump), encoding="utf-8")
    acceptance.scan(plan, FakeRecorder())
    assert plan.state["secret_scan"]["browser_storage_entries"] == 2 and plan.state["secret_scan"]["hits"] == 0
    dump["local"]["leak"] = "wv-canary-x1-unit"
    (storage / "J1-1440x900-builder.json").write_text(json.dumps(dump), encoding="utf-8")
    with pytest.raises(acceptance.StageFailed, match="found a secret"):
        acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1] == {
        "id": "J9.4",
        "status": "failed",
        "seconds": scan_steps(plan)[-1]["seconds"],
        "error": "The canary scan found a secret in 1 file(s)",
        "source": "scan",
    }


def test_scan_steps_are_judged_by_the_scan_stage():
    journeys = [
        {"id": "J9", "status": "failed", "steps": [{"n": 1, "status": "passed"}, {"n": 4, "status": "not_run"}]}
    ]
    assert acceptance.failed_journeys(journeys, ("run",)) == []
    assert acceptance.failed_journeys(journeys, ("run", "scan")) == ["J9"]


def test_each_person_a_studio_journey_signed_in_as_needs_a_storage_dump(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = journeys_plan(tmp_path / "run")
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, True))
    (plan.evidence / "steps.jsonl").write_text(
        json.dumps({"id": "J9.2", "status": "passed", "viewport": "1440x900"}) + "\n", encoding="utf-8"
    )
    people = [{"journey": "J9", "viewport": "1440x900", "person": name} for name in ("builder", "viewer")]
    (plan.private / acceptance.STUDIO_PEOPLE).write_text(
        "".join(json.dumps(person) + "\n" for person in people), encoding="utf-8"
    )
    storage = plan.private / "browser-storage"
    storage.mkdir()
    dump = {"local": {"a": "1"}, "session": {}, "indexed_db": []}
    (storage / "J9-1440x900-builder.json").write_text(json.dumps(dump), encoding="utf-8")
    with pytest.raises(acceptance.StageFailed, match="captured for J9-1440x900-viewer"):
        acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1]["status"] == "failed"
    assert plan.state["secret_scan"]["browser_storage_entries"] == 1
    (storage / "J9-1440x900-viewer.json").write_text(json.dumps(dump), encoding="utf-8")
    acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1]["status"] == "partial"
    assert plan.state["secret_scan"]["browser_storage_entries"] == 2


def test_the_studio_harness_writes_the_people_and_dumps_the_scan_expects():
    harness = (acceptance.ROOT / "studio/tests/acceptance/harness.ts").read_text(encoding="utf-8")
    assert f'const STUDIO_PEOPLE = "{acceptance.STUDIO_PEOPLE}";' in harness
    assert "dump: signed.dump," in harness
    assert "const base = signed.dump;" in harness and "`${base}.json`" in harness
    assert "dumpOriginProblem(dump.origin, signed.host)" in harness
    assert f'const AFTER_REMOVAL = "{acceptance.acceptance_evidence.AFTER_REMOVAL}";' in harness
    assert "`${base}${AFTER_REMOVAL}.json`" in harness
    # Each dump lists IndexedDB databases exactly while the scan checks storage keys.
    assert f'const KEY_CHECK = "{acceptance.SCAN_CHECK}";' in harness
    assert "storageKeyCheckOn(enablement().steps, KEY_CHECK)" in harness
    assert f"{acceptance.acceptance_evidence.DATABASES}: databases" in harness
    # The second dump waits for the purge: no account key left, or 10 s.
    purge = harness.index("storageAccountKeys(await dumpStorage(signed.page))")
    assert purge < harness.index("`${base}${AFTER_REMOVAL}.json`")


def test_a_second_sign_in_of_the_same_person_needs_its_own_dump(tmp_path):
    plan = journeys_plan(tmp_path)
    lines = [
        {"journey": "J9", "viewport": "1440x900", "person": "viewer", "dump": "J9-1440x900-viewer"},
        {"journey": "J9", "viewport": "1440x900", "person": "viewer", "dump": "J9-1440x900-viewer-2"},
        {"journey": "J1", "viewport": "600x500", "person": "builder"},
    ]
    (plan.private / acceptance.STUDIO_PEOPLE).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    assert acceptance.studio_people(plan) == ["J1-600x500-builder", "J9-1440x900-viewer", "J9-1440x900-viewer-2"]


@pytest.mark.parametrize("profile", ["ai", "cluster", "k3d"])
def test_a_missing_storage_dump_fails_the_scan_in_profiles_without_j9(tmp_path, monkeypatch, profile):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, True))
    plan = journeys_plan(tmp_path / "run")
    plan.profile = profile
    (plan.private / acceptance.STUDIO_PEOPLE).write_text(
        json.dumps({"journey": "J4", "viewport": "1440x900", "person": "builder"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(acceptance.StageFailed, match="captured for J4-1440x900-builder"):
        acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan) == []


def test_the_scan_checks_storage_keys_once_the_editor_prefixes_them(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    path = journeys_map(tmp_path, True)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"editor-contracts" = false', '"editor-contracts" = true'),
        encoding="utf-8",
    )
    monkeypatch.setattr(acceptance, "JOURNEYS", path)
    plan = journeys_plan(tmp_path / "run")
    (plan.evidence / "steps.jsonl").write_text(
        json.dumps({"id": "J9.2", "status": "passed", "viewport": "1440x900"}) + "\n", encoding="utf-8"
    )
    (plan.private / acceptance.STUDIO_PEOPLE).write_text(
        json.dumps({"journey": "J9", "viewport": "1440x900", "person": "viewer", "dump": "J9-1440x900-viewer"}) + "\n",
        encoding="utf-8",
    )
    storage = plan.private / "browser-storage"
    storage.mkdir()
    key = (
        "p0123456789abcdef:fedcba9876543210:11111111-2222-3333-4444-555555555555:66666666-7777-8888-9999-000000000000:x"
    )
    before = {"local": {key: "v", "ui:w": "1"}, "session": {}, "indexed_db": [], "indexed_db_databases": []}
    (storage / "J9-1440x900-viewer.json").write_text(json.dumps(before), encoding="utf-8")
    (storage / "J9-1440x900-viewer.after-removal.json").write_text(json.dumps(before), encoding="utf-8")
    with pytest.raises(acceptance.StageFailed, match="1 account key"):
        acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1]["status"] == "failed"
    after = {"local": {"ui:w": "1"}, "session": {}, "indexed_db": [], "indexed_db_databases": []}
    (storage / "J9-1440x900-viewer.after-removal.json").write_text(json.dumps(after), encoding="utf-8")
    acceptance.scan(plan, FakeRecorder())
    assert scan_steps(plan)[-1]["status"] == "passed" and "skipped_checks" not in scan_steps(plan)[-1]


def test_the_studio_harness_registers_each_person_before_signing_in():
    # A sign-in that fails must still leave its people line and its place in close(), or the scan would neither
    # expect nor see that context's storage.
    harness = (acceptance.ROOT / "studio/tests/acceptance/harness.ts").read_text(encoding="utf-8")
    body = harness[harness.index("  async signIn(name: string)") :]
    body = body[: body.index("\n  }\n")]
    order = [
        body.index("this.#people.push(signed);"),
        body.index("appendPrivate(\n      STUDIO_PEOPLE,"),
        body.index("await session(page)"),
        body.index("await pair(page"),
        body.index("await connectAndSignIn(page"),
        body.index("signed.signedIn = true;"),
    ]
    assert order == sorted(order)


def test_platform_up_approves_the_acme_service_and_the_identity_alias():
    # The identity alias names the control plane (Keycloak, whose network namespace the API shares) by an approved
    # origin, so J9.3 sees the control-plane rule refuse it although its entry exists. Acme stays first: the
    # Studio suites read it as the Acme origin.
    from firefly_weave.sdk.platform_origins import fixture_origin

    assert acceptance.FIXTURE_ORIGINS == (acceptance.FIXTURE_ORIGIN, acceptance.IDENTITY_ORIGIN)
    assert acceptance.IDENTITY_ORIGIN == "http://identity.acceptance.test:8080"
    assert [fixture_origin(origin) for origin in acceptance.FIXTURE_ORIGINS] == list(acceptance.FIXTURE_ORIGINS)


def test_scan_finds_the_identity_providers_admin_secret_outside_its_own_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    plan = journeys_plan(tmp_path / "run")
    monkeypatch.setattr(acceptance, "JOURNEYS", journeys_map(tmp_path, False))
    plan.platform_dir.mkdir(parents=True)
    admin = "kc-admin-" + "e" * 32
    (plan.platform_dir / "identity.env").write_text(f"WEAVE_KC_ADMIN_SECRET={admin}\n", encoding="utf-8")
    # Its own file is where it belongs; anywhere else is a leak.
    acceptance.scan(plan, FakeRecorder())
    assert plan.state["secret_scan"]["hits"] == 0
    (plan.evidence / "log.txt").write_text(f"token request with {admin}\n", encoding="utf-8")
    with pytest.raises(acceptance.StageFailed, match="found a secret"):
        acceptance.scan(plan, FakeRecorder())


def test_the_studio_harness_redacts_the_identity_providers_admin_secret():
    harness = (acceptance.ROOT / "studio/tests/acceptance/harness.ts").read_text(encoding="utf-8")
    body = harness[harness.index("function secrets(): string[] {") :]
    body = body[: body.index("\n}\n")]
    assert "adminSecret()" in body


def test_a_studio_step_whose_page_left_http_or_its_hosts_fails_itself():
    # finish() fails the test, but the step that navigated must not keep a passed record.
    harness = (acceptance.ROOT / "studio/tests/acceptance/harness.ts").read_text(encoding="utf-8")
    step = harness[harness.index("  async step(stepId: string") :]
    step = step[: step.index("\n  }\n")]
    body, after = step.index("await body();"), step.index("this.#navigationProblems.length > navigated")
    assert body < after
    sign_in = harness[harness.index("  async signIn(name: string)") :]
    assert "this.#navigationProblems.push(" in sign_in[: sign_in.index("\n  }\n")]


class ImageRecorder:
    """Answers the Docker commands of the operations image steps and records them."""

    def __init__(self):
        self.commands = []

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.commands.append(argv)
        stdout = ""
        if "--iidfile" in argv:
            Path(argv[argv.index("--iidfile") + 1]).write_text("sha256:" + ("1" if "runner" in argv else "2") * 64)
        elif argv[-2:] == ["--format", "{{.Id}}"]:
            stdout = argv[-3]
        elif "inspect" in argv:
            stdout = "layers and config"
        elif argv[3:5] == ["ps", "--all"] or "ls" in argv:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_prepare_pins_the_registry_and_docker_cli_images(tmp_path):
    from firefly_weave.sdk import platform_operations

    versions = acceptance.acceptance_journeys.load_versions(acceptance.VERSIONS)
    assert versions["images"]["registry"] == platform_operations.REGISTRY_IMAGE
    assert versions["images"]["docker_cli"] == platform_operations.DOCKER_CLI_IMAGE
    assert f"COPY --from={platform_operations.DOCKER_CLI_IMAGE} " in (ROOT / "Dockerfile").read_text()
    # The operations images weave platform up builds are built here from the same inputs, so they are cache hits.
    plan = blocked_plan(tmp_path)
    recorder = ImageRecorder()
    release = tmp_path / "release"
    inventory = acceptance._operations_images(plan, recorder, plan.docker(), release)
    builds = [argv for argv in recorder.commands if "build" in argv]
    assert [argv[argv.index("--target") + 1] for argv in builds] == ["runner", "operations-worker"]
    assert builds[1][builds[1].index("-f") + 1] == str(release / "images" / "worker.Dockerfile")
    assert [item["ref"] for item in inventory] == [
        "firefly-weave runner (weave platform up inputs)",
        "firefly-weave sleep fixture worker (weave platform up inputs)",
    ]


def test_down_removes_the_workers_project_and_the_operations_images(tmp_path):
    plan = blocked_plan(tmp_path)
    plan.state["egress_rules"] = False
    platform = plan.platform_dir
    platform.mkdir(parents=True)
    (platform / "platform.json").write_text(json.dumps({"id": "a" * 24}))
    runner, worker = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    (platform / "operations.json").write_text(json.dumps({"runner_image": runner, "worker_image_id": worker}))
    assert acceptance._projects(plan) == [
        "weave-local-" + "a" * 24,
        "weave-local-" + "a" * 24 + "-workers",
        f"weave-acc-{RUN}",
    ]
    recorder = ImageRecorder()
    acceptance.down(plan, recorder)
    removed = [argv for argv in recorder.commands if argv[3:5] == ["image", "rm"]]
    assert removed == [[*plan.docker(), "image", "rm", "--force", runner, worker]]
    project_filters = [argv[-1] for argv in recorder.commands if "ps" in argv and "--quiet" in argv]
    assert "label=com.docker.compose.project=weave-local-" + "a" * 24 + "-workers" in project_filters


CLUSTER = f"weave-acc-{RUN}"
CONTEXT = "colima-weave-tests"
CLUSTER_SUBNET = "172.19.0.0/16"
SERVER_VERSIONS = {"kind": "v1.37.0", "k3d": "v1.37.1+k3s1"}
WEAVE = [sys.executable, "-m", "firefly_weave.cli.main", "platform", "--directory"]


def cluster_network(provider):
    return f"weave-kind-{CLUSTER}" if provider == "kind" else f"k3d-{CLUSTER}"


class ClusterRecorder:
    """Answers the commands of prepare, up, collect and down as Docker, the weave CLI, kind, k3d and kubectl would,
    and records each with its environment. An egress probe exits 1 (blocked) unless its network is in ``open``;
    each runner's log prints ``logs``."""

    def __init__(self, open_networks=(), logs="runner started\n"):
        self.calls, self.open, self.logs = [], set(open_networks), logs

    def run(self, argv, *, env=None, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append((argv, env))
        tool = Path(argv[0]).name
        code, stdout = 0, ""
        if "--iidfile" in argv:
            Path(argv[argv.index("--iidfile") + 1]).write_text("sha256:" + "1" * 64)
        elif "{{.Server.Version}}" in argv:
            stdout = "28.5.1\n"
        elif argv[-3:] == ["compose", "version", "--short"]:
            stdout = "2.39.4\n"
        elif argv[-2:] == ["--format", "{{.Id}}"]:
            stdout = "sha256:" + "2" * 64 + "\n"
        elif acceptance.IMAGE_CONTENT_FORMAT in argv:
            stdout = '["sha256:' + "3" * 64 + '"]{"User":"65532"}\n'
        elif argv[:5] == WEAVE[:5] and argv[6:8] == ["cluster", "create"]:
            provider, name = argv[argv.index("--provider") + 1], argv[argv.index("--name") + 1]
            created = {"cluster": name, "context": f"{provider}-{name}", "network": cluster_network(provider)}
            stdout = json.dumps(created) + "\n"
        elif tool.startswith("kind-") and argv[1:] == ["version"]:
            stdout = "kind v0.33.0 go1.25.4 linux/amd64\n"
        elif tool.startswith("k3d-") and argv[1:] == ["version"]:
            stdout = "k3d version v5.9.0\nk3s version v1.33.6-k3s1 (default)\n"
        elif tool.startswith("kubectl-") and "version" in argv:
            server = SERVER_VERSIONS[argv[argv.index("--context") + 1].split("-")[0]]
            stdout = json.dumps({"clientVersion": {"gitVersion": "v1.37.2"}, "serverVersion": {"gitVersion": server}})
        elif argv[3:5] == ["network", "inspect"]:
            subnets = [{"Subnet": "fc00:f853:ccd:e793::/64"}, {"Subnet": CLUSTER_SUBNET}]
            stdout = json.dumps([{"IPAM": {"Config": subnets}}])
        elif "run" in argv and "--rm" in argv:
            network = argv[argv.index("--network") + 1] if "--network" in argv else "bridge"
            code = 0 if network in self.open else 1
        elif argv[3:5] == ["ps", "--all"] and "--quiet" in argv:
            stdout = "c0ffee\n"
        elif tool.startswith("kubectl-") and "namespaces" in argv:
            stdout = "weave-system weave-workers-o-j4"
        elif tool.startswith("kubectl-") and "deployments" in argv:
            stdout = "deployment.apps/weave-runner-weave-workers-o-j4\n"
        elif tool.startswith("kubectl-") and "logs" in argv:
            stdout = self.logs
        elif tool.startswith("kubectl-") and ("get" in argv or "describe" in argv):
            stdout = f"objects in {argv[argv.index('--namespace') + 1]}\n"
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")


def cluster_plan(tmp_path, monkeypatch, profile="cluster", block_egress=False):
    """A new run of ``profile`` whose prepare stage finds every tool and the Studio dependencies."""
    checkout = tmp_path / "checkout"
    (checkout / "studio/node_modules").mkdir(parents=True)
    monkeypatch.setattr(acceptance, "ROOT", checkout)
    monkeypatch.setattr(acceptance.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan = acceptance.Plan(tmp_path / RUN, RUN, profile, CONTEXT, block_egress, False, {"profile": profile})
    plan.root.mkdir()
    plan.private.mkdir(mode=0o700)
    plan.evidence.mkdir(mode=0o700)
    return plan


def pinned_kubectl(plan):
    """kubectl where weave platform cluster create fetches it, in the run's private cluster folder."""
    from firefly_weave.sdk import platform_cluster_tools

    path = platform_cluster_tools._path(plan.private / "cluster", "kubectl")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("")


def is_cluster_command(argv, plan):
    """The weave cluster commands and the pinned kind, k3d and kubectl the run's private cluster folder holds."""
    weave = argv[:5] == WEAVE[:5] and argv[6] == "cluster"
    return weave or Path(argv[0]).parent == plan.private / "cluster" / "tools"


@pytest.mark.parametrize(("profile", "provider", "node"), [("cluster", "kind", "kind_node"), ("k3d", "k3d", "k3s")])
def test_the_cluster_profile_creates_its_cluster_without_connecting_it(tmp_path, monkeypatch, profile, provider, node):
    plan = cluster_plan(tmp_path, monkeypatch, profile)
    recorder = ClusterRecorder()
    acceptance.prepare(plan, recorder)
    commands = [argv for argv, _ in recorder.calls]
    folder = str(plan.private / "cluster")
    create = [*WEAVE, folder, "cluster", "create", "--provider", provider, "--name", CLUSTER, "--no-connect"]
    create += ["--context", CONTEXT, "--output", "json"]
    assert [argv for argv in commands if argv[6:8] == ["cluster", "create"]] == [create]
    assert not any("connect" in argv for argv in commands)
    # The folder that keeps the pinned tools and their logs is the run's own, private, and not the platform's.
    assert stat.S_IMODE((plan.private / "cluster").stat().st_mode) == 0o700
    assert not plan.platform_dir.exists()
    node_image = acceptance.acceptance_journeys.load_versions(acceptance.VERSIONS)["cluster"]["images"][node]
    assert plan.state["cluster"] == {
        "provider": provider,
        "name": CLUSTER,
        "context": f"{provider}-{CLUSTER}",
        "network": cluster_network(provider),
        "subnet": CLUSTER_SUBNET,
        "node_image": node_image,
        "tool_version": "v0.33.0" if provider == "kind" else "v5.9.0",
        "kubernetes_version": SERVER_VERSIONS[provider],
    }
    assert json.loads((plan.root / "run.json").read_text(encoding="utf-8"))["cluster"]["name"] == CLUSTER
    assert node_image in [item["ref"] for item in plan.state["inventory"]]
    # The system pods run before up can block egress: k3s pulls their images from the internet.
    waits = [argv for argv in commands if "wait" in argv and "deployment/coredns" in argv]
    assert len(waits) == 1 and commands.index(waits[0]) > commands.index(create)


def cluster_state(provider="kind"):
    return {
        "provider": provider,
        "name": CLUSTER,
        "context": f"{provider}-{CLUSTER}",
        "network": cluster_network(provider),
        "subnet": CLUSTER_SUBNET,
        "node_image": "kindest/node:v1.37.0@sha256:" + "a" * 64,
        "tool_version": "v0.33.0",
        "kubernetes_version": "v1.37.0",
    }


def test_down_deletes_the_cluster_before_the_platform(tmp_path, monkeypatch):
    plan = cluster_plan(tmp_path, monkeypatch)
    plan.state.update(cluster=cluster_state("k3d"))
    (plan.private / "cluster").mkdir(mode=0o700)
    plan.platform_dir.mkdir(parents=True)
    (plan.platform_dir / "platform.json").write_text(json.dumps({"id": "b" * 24}))
    recorder = ClusterRecorder()
    acceptance.down(plan, recorder)
    commands = [argv for argv, _ in recorder.calls]
    delete = [*WEAVE, str(plan.private / "cluster"), "cluster", "delete", "--provider", "k3d", "--name", CLUSTER]
    delete += ["--context", CONTEXT, "--output", "json"]
    platform = [index for index, argv in enumerate(commands) if argv[3:5] == ["rm", "--force"]]
    assert platform and commands.index(delete) < platform[0]
    assert plan.state["resources_left"] == [] and not plan.private.exists()


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("cluster", {"kind": "v0.33.0", "k3d": None, "kubernetes": "v1.37.0"}),
        ("k3d", {"kind": None, "k3d": "v5.9.0", "kubernetes": "v1.37.1+k3s1"}),
    ],
)
def test_the_evidence_names_the_cluster_tool_and_kubernetes_versions(tmp_path, monkeypatch, profile, expected):
    plan = cluster_plan(tmp_path, monkeypatch, profile)
    acceptance.prepare(plan, ClusterRecorder())
    # The summary reads the checkout's own versions and commit, and validates against the evidence schema.
    monkeypatch.setattr(acceptance, "ROOT", ROOT)
    environment = acceptance.summarize(plan, every_stage_passed(plan))["environment"]
    assert {key: environment[key] for key in ("kind", "k3d", "kubernetes")} == expected


def test_a_run_of_another_profile_makes_no_cluster(tmp_path, monkeypatch):
    plan = cluster_plan(tmp_path, monkeypatch, "pr")
    recorder = ClusterRecorder()
    acceptance.prepare(plan, recorder)
    assert "cluster" not in plan.state and not (plan.private / "cluster").exists()
    assert not (plan.private / "kubeconfig").exists()
    monkeypatch.setattr(acceptance, "ROOT", ROOT)
    environment = acceptance.summarize(plan, every_stage_passed(plan))["environment"]
    assert (environment["kind"], environment["k3d"], environment["kubernetes"]) == (None, None, None)
    acceptance.down(plan, recorder)
    assert not any(is_cluster_command(argv, plan) for argv, _ in recorder.calls)


def test_a_long_run_id_is_refused_for_cluster_profiles(tmp_path, monkeypatch, capsys):
    stub_stages(monkeypatch, lambda plan, recorder: None)
    long = RUN + "0"
    for profile in ("cluster", "k3d"):
        argv = ["--profile", profile, "--root", str(tmp_path), "--run-id", long, "--docker-context", CONTEXT]
        assert acceptance.main(argv) == 2
        refusal = "Cluster profiles need a run ID of at most 22 characters, so the cluster name fits."
        assert refusal in capsys.readouterr().err
    assert not (tmp_path / long).exists()
    # 22 characters make a 32-character cluster name; other profiles keep longer run IDs.
    for profile, run_id in (("cluster", RUN), ("pr", long)):
        argv = ["--profile", profile, "--root", str(tmp_path / profile), "--run-id", run_id]
        assert acceptance.main([*argv, "--docker-context", CONTEXT, "--stages", "prepare"]) == 0


@pytest.mark.parametrize("profile", ["cluster", "k3d"])
def test_the_kubeconfig_stays_in_the_runs_private_folder(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "developer" / "config"))
    monkeypatch.setenv("WEAVE_API_URL", "http://developer.invalid")
    plan = cluster_plan(tmp_path, monkeypatch, profile)
    recorder = ClusterRecorder()
    acceptance.prepare(plan, recorder)
    kubeconfig = plan.private / "kubeconfig"
    assert stat.S_IMODE(kubeconfig.stat().st_mode) == 0o600
    pinned_kubectl(plan)
    acceptance.collect(plan, recorder)
    acceptance.down(plan, recorder)
    commands = [(argv, env) for argv, env in recorder.calls if is_cluster_command(argv, plan)]
    kinds = {argv[7] if argv[:5] == WEAVE[:5] else Path(argv[0]).name.split("-")[0] for argv, _ in commands}
    assert any("logs" in argv for argv, _ in commands)
    assert kinds == {"create", "delete", "kubectl", "kind" if profile == "cluster" else "k3d"}
    for argv, env in commands:
        assert env is not None and env["KUBECONFIG"] == str(kubeconfig), argv
        assert not [key for key in env if key.startswith("WEAVE_")], argv


def test_blocked_egress_covers_the_cluster_network(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    plan = cluster_plan(tmp_path, monkeypatch, block_egress=True)
    plan.state.update(images={"python": "python:3.12-slim"}, cluster=cluster_state())
    recorder = ClusterRecorder()
    acceptance.up(plan, recorder)
    rules = [argv for argv, _ in recorder.calls if argv[:3] == ["sudo", "-n", "iptables"]]
    chain = ["sudo", "-n", "iptables", "-w", "-A", "WEAVE-ACCEPTANCE"]
    own = [*chain, "-s", CLUSTER_SUBNET, "-d", CLUSTER_SUBNET, "-j", "RETURN"]
    rest = [*chain, "-s", CLUSTER_SUBNET, "-j", "REJECT"]
    private = [*chain, "-d", "10.0.0.0/8", "-j", "RETURN"]
    # The cluster's nodes reach only their own network, where connect puts the platform API and the registry.
    assert rules.index(own) < rules.index(rest) < rules.index(private)
    probes = [argv for argv, _ in recorder.calls if "--rm" in argv]
    assert [argv[argv.index("--network") + 1] for argv in probes if "--network" in argv] == [cluster_network("kind")]
    # A node that still reaches a public address fails the stage.
    plan = cluster_plan(tmp_path / "open", monkeypatch, block_egress=True)
    plan.state.update(images={"python": "python:3.12-slim"}, cluster=cluster_state())
    with pytest.raises(acceptance.StageFailed, match=cluster_network("kind")):
        acceptance.up(plan, ClusterRecorder(open_networks={cluster_network("kind")}))


def test_collect_keeps_the_clusters_objects_and_runner_logs_redacted(tmp_path, monkeypatch):
    plan = cluster_plan(tmp_path, monkeypatch)
    plan.state.update(cluster=cluster_state())
    (plan.private / "cluster").mkdir(mode=0o700)
    pinned_kubectl(plan)
    (plan.private / "kubeconfig").write_text("")
    canary = "wv-canary-" + "c" * 32
    (plan.private / "canaries.json").write_text(json.dumps({"acme-api-key": canary}), encoding="utf-8")
    recorder = ClusterRecorder(logs=f"refused a request that carried {canary}\n")
    with pytest.raises(acceptance.StageFailed, match="redacted"):
        acceptance.collect(plan, recorder)
    folder = plan.evidence / "cluster"
    assert sorted(path.name for path in folder.iterdir()) == [
        "weave-runner-weave-workers-o-j4.log",
        "weave-system.describe.txt",
        "weave-system.get.txt",
        "weave-workers-o-j4.describe.txt",
        "weave-workers-o-j4.get.txt",
    ]
    log = (folder / "weave-runner-weave-workers-o-j4.log").read_text(encoding="utf-8")
    assert canary not in log and "refused a request that carried [redacted]" in log
    assert (folder / "weave-workers-o-j4.get.txt").read_text(encoding="utf-8") == "objects in weave-workers-o-j4\n"
    gets = [argv[3:] for argv, _ in recorder.calls if "get" in argv and "wide" in argv]
    objects = "all,networkpolicy,configmap,endpointslice"
    assert ["--namespace", "weave-system", "get", objects, "--output", "wide"] in gets
    # Output without a run secret passes.
    acceptance.collect(plan, ClusterRecorder())
