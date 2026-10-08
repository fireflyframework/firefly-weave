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
