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

"""weave platform ai: option rules, confirmations without a terminal, and readable results."""

import json

import pytest
from click.testing import CliRunner

from firefly_weave.cli import platform as platform_cli
from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform_ai

SUMMARY = {
    "ok": True,
    "stage": "ready",
    "mode": "container",
    "endpoint": "http://ollama:11434/v1",
    "models": ["qwen2.5:1.5b"],
    "approval": "served",
    "connection": "ollama-local",
    "connection_revision_id": "77777777-7777-4777-8777-777777777777",
    "release_id": "44444444-4444-4444-8444-444444444444",
    "weave_ai": "not_available",
    "test": {"ok": True, "code": "ok", "latency_ms": 900, "model": "qwen2.5:1.5b", "tool_calling": "supported"},
    "warnings": [],
    "changed": ["ollama", "models"],
    "worker": {"presence": "recent"},
    "smoke": None,
    "next": ["weave platform --directory /p ai status"],
}
STATUS = {
    "ok": True,
    "enabled": True,
    "stage": "ready",
    "mode": "container",
    "endpoint": "http://ollama:11434/v1",
    "models": {
        "approval": "served",
        "approved": [],
        "served": [{"name": "qwen2.5:1.5b", "tools": "yes", "context_tokens": 32768}],
    },
    "gateway": {"state": "running"},
    "worker": {"state": "running", "presence": "recent", "last_seen_at": "2026-10-08T10:00:00+00:00"},
    "ollama": {"state": "running"},
    "last_test": SUMMARY["test"],
    "warnings": ["CPU only: Docker on macOS cannot use the Apple GPU; --ollama host is faster when Ollama runs here."],
}


def weave(tmp_path, *args, **invoke):
    return CliRunner().invoke(cli, ["platform", "--directory", str(tmp_path / "platform"), "ai", *args], **invoke)


def test_ollama_needs_a_value():
    result = CliRunner().invoke(cli, ["platform", "ai", "enable", "--ollama"])
    assert result.exit_code == 2 and "WV-CLI-USAGE" in result.output


@pytest.mark.parametrize("choice", [[], ["--ollama", "auto", "--ollama-url", "http://ollama.acceptance.test:11434"]])
def test_exactly_one_ollama_choice_is_required(tmp_path, choice):
    result = weave(tmp_path, "enable", *choice, "--output", "json")
    assert result.exit_code == 2 and "exactly one of --ollama MODE or --ollama-url URL" in result.output


def test_without_a_terminal_only_yes_accepts(monkeypatch, tmp_path):
    seen = {}

    def enable(directory, **kwargs):
        seen.update(kwargs)
        kwargs["confirm"]("Continue?")
        return SUMMARY

    monkeypatch.setattr(platform_ai, "enable", enable)
    refused = weave(tmp_path, "enable", "--ollama", "container", "--model", "qwen2.5:1.5b", "--output", "json")
    assert refused.exit_code == 2 and "--yes" in refused.output
    accepted = weave(
        tmp_path, "enable", "--ollama", "container", "--model", "qwen2.5:1.5b", "--verify", "--yes", "--output", "json"
    )
    assert accepted.exit_code == 0 and json.loads(accepted.output)["stage"] == "ready"
    assert seen["ollama_mode"] == "container" and seen["models"] == ("qwen2.5:1.5b",) and seen["verify"] is True


def test_enable_text_reports_the_test_and_what_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_ai, "enable", lambda directory, **kwargs: SUMMARY)
    result = weave(tmp_path, "enable", "--ollama-url", "http://ollama.acceptance.test:11434", "--yes")
    assert result.exit_code == 0, result.output
    assert "AI is ready on this platform (development only)." in result.output
    assert "Test: qwen2.5:1.5b answered in 0.9 s and can call tools." in result.output
    assert "Changed: ollama, models" in result.output


def test_status_disable_and_models_call_through(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(platform_ai, "status", lambda directory: STATUS)

    def disable(directory, *, remove_model_data, notice):
        calls.append(("disable", remove_model_data))
        return {"ok": True, "disabled": True, "message": "AI services stopped."}

    def approve(directory, *, provider, model=None, served=False):
        calls.append(("approve", provider, model, served))
        approval = {"approval": "served", "approved": []} if served else {"approval": "listed", "approved": [model]}
        return {"ok": True, **approval, "changed": True, "message": "Applied."}

    def remove(directory, *, provider, model):
        calls.append(("remove", provider, model))
        return {"ok": True, "approval": "listed", "approved": [], "changed": True, "message": "Applied."}

    def pull(directory, name, *, confirm, progress):
        calls.append(("pull", name, confirm("Download?")))
        return {"ok": True, "pulled": name, "served": []}

    monkeypatch.setattr(platform_ai, "disable", disable)
    monkeypatch.setattr(platform_ai, "models_approve", approve)
    monkeypatch.setattr(platform_ai, "models_remove", remove)
    monkeypatch.setattr(
        platform_ai,
        "models_refresh",
        lambda directory: {"ok": True, "served": [], "changed": False, "context_tokens": 8192, "warnings": []},
    )
    monkeypatch.setattr(platform_ai, "models_pull", pull)
    status = weave(tmp_path, "status")
    assert "AI gateway: running" in status.output and "Agentic worker: running · presence recent" in status.output
    assert "Warning: CPU only" in status.output
    assert weave(tmp_path, "disable", "--remove-model-data").exit_code == 0
    assert (
        weave(tmp_path, "models", "approve", "--provider", "openai-chat", "--model", "weave-missing:1b").exit_code == 0
    )
    assert weave(tmp_path, "models", "approve", "--provider", "openai-chat", "--served").exit_code == 0
    assert weave(tmp_path, "models", "remove", "--provider", "openai-chat", "--model", "qwen2.5:1.5b").exit_code == 0
    assert weave(tmp_path, "models", "refresh", "--output", "json").exit_code == 0
    assert weave(tmp_path, "models", "pull", "qwen3:4b", "--yes").exit_code == 0
    assert calls == [
        ("disable", True),
        ("approve", "openai-chat", "weave-missing:1b", False),
        ("approve", "openai-chat", None, True),
        ("remove", "openai-chat", "qwen2.5:1.5b"),
        ("pull", "qwen3:4b", True),
    ]


def test_help_lists_the_ai_commands():
    result = CliRunner().invoke(cli, ["platform", "ai", "--help"])
    assert result.exit_code == 0
    for command in ("enable", "status", "disable", "models"):
        assert command in result.output


@pytest.mark.parametrize(("typed", "accepted"), [("y\n", True), ("n\n", False), ("\n", False), ("", False)])
def test_a_terminal_asks_on_standard_error_and_a_closed_prompt_declines(monkeypatch, tmp_path, typed, accepted):
    monkeypatch.setattr(platform_cli, "_interactive_stdin", lambda: True)
    answers = []

    def pull(directory, name, *, confirm, progress):
        answers.append(confirm("Download qwen3:4b?"))
        return {"ok": True, "pulled": name, "served": []}

    monkeypatch.setattr(platform_ai, "models_pull", pull)
    result = weave(tmp_path, "models", "pull", "qwen3:4b", "--output", "json", input=typed)
    assert result.exit_code == 0, result.output
    assert answers == [accepted]
    assert json.loads(result.stdout) == {"ok": True, "pulled": "qwen3:4b", "served": []}
    assert "Download qwen3:4b?" in result.stderr and "Download" not in result.stdout


def test_progress_goes_to_standard_error_in_text_and_nowhere_in_json(monkeypatch, tmp_path):
    def enable(directory, **kwargs):
        kwargs["progress"]("Pulling qwen2.5:1.5b")
        return SUMMARY

    monkeypatch.setattr(platform_ai, "enable", enable)
    text = weave(tmp_path, "enable", "--ollama", "container", "--yes")
    assert text.exit_code == 0, text.output
    assert "Pulling qwen2.5:1.5b" in text.stderr and "Pulling" not in text.stdout
    machine = weave(tmp_path, "enable", "--ollama", "container", "--yes", "--output", "json")
    assert machine.exit_code == 0, machine.output
    assert machine.stderr == "" and json.loads(machine.stdout) == SUMMARY


def test_model_results_show_their_warnings(monkeypatch, tmp_path):
    warning = "qwen3:0.6b reports 4,096 tokens of context, so prompts to this endpoint are limited to 4,096 tokens."
    served = [{"name": "qwen3:0.6b", "tools": "yes", "context_tokens": 4096}]
    monkeypatch.setattr(
        platform_ai,
        "models_refresh",
        lambda directory: {
            "ok": True,
            "served": served,
            "changed": True,
            "context_tokens": 4096,
            "warnings": [warning],
        },
    )
    monkeypatch.setattr(
        platform_ai,
        "models_approve",
        lambda directory, *, provider, model=None, served=False: {
            "ok": True,
            "approval": "listed",
            "approved": [model],
            "changed": True,
            "warnings": [warning],
            "message": "Applied.",
        },
    )
    refreshed = weave(tmp_path, "models", "refresh")
    assert "Served: qwen3:0.6b" in refreshed.output and f"Warning: {warning}" in refreshed.output
    approved = weave(tmp_path, "models", "approve", "--provider", "openai-chat", "--model", "qwen3:0.6b")
    assert "Approved: qwen3:0.6b" in approved.output and f"Warning: {warning}" in approved.output


def test_status_before_enable_points_to_the_command(monkeypatch, tmp_path):
    next_step = "weave platform --directory /p ai enable --ollama auto"
    monkeypatch.setattr(
        platform_ai,
        "status",
        lambda directory: {"ok": True, "enabled": False, "stage": "not_enabled", "next": [next_step]},
    )
    result = weave(tmp_path, "status")
    assert result.exit_code == 0 and result.output == f"AI is not enabled. Run: {next_step}\n"


def test_platform_errors_are_plain_and_exit_with_a_usage_code(monkeypatch, tmp_path):
    def disable(directory, *, remove_model_data, notice):
        raise platform_ai.local.PlatformError("AI was never enabled in this installation; nothing was changed.")

    monkeypatch.setattr(platform_ai, "disable", disable)
    result = weave(tmp_path, "disable")
    assert result.exit_code == 2 and "AI was never enabled in this installation; nothing was changed." in result.output
