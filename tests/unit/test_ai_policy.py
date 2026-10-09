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

"""The AI policy describes models and endpoints; reachability comes only from private origins."""

import json
import logging
import os
import sys

import pytest

from firefly_weave import ai_policy
from firefly_weave import private_origins as po

OLLAMA = {
    "id": "ollama-local",
    "label": "Ollama (Weave-managed)",
    "url": "http://ollama:11434/v1",
    "providers": ["openai-chat"],
    "compat": "ollama",
    "credential": "none",
    "models": "served",
    "contextTokens": 8192,
    "structuredOutput": "native",
    "maxOutputTokens": 4096,
}
OPENAI = {
    "id": "openai",
    "label": "OpenAI",
    "url": "https://api.openai.com/v1",
    "providers": ["openai-chat", "openai-responses"],
    "credential": "required",
    "models": ["gpt-5-mini", "gpt-5"],
    "contextTokens": 128000,
}


def origins(credentials="none", origin="http://ollama:11434"):
    entry = po.PrivateOrigin(origin=origin, purpose="model", networks=("10.246.21.0/24",), credentials=credentials)
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])


def document(*endpoints):
    return json.dumps({"version": 2, "endpoints": list(endpoints)}).encode()


def test_version_two_describes_endpoints_models_and_context():
    policy = ai_policy.parse(document(OLLAMA, OPENAI), origins())
    ollama = policy.entry_for("http://ollama:11434/v1")
    assert ollama is not None and ollama.served and ollama.context_tokens == 8192 and ollama.output_mode == "native"
    assert ollama.origin == "http://ollama:11434"
    assert ollama.approves("openai-chat", "anything-served:1b")
    assert not ollama.approves("anthropic", "qwen3:4b")
    openai = policy.entry_for("https://api.openai.com/v1")
    assert openai is not None and openai.output_mode == "tool"
    assert openai.approves("openai-responses", "gpt-5") and not openai.approves("openai-chat", "gpt-4o")
    assert policy.approves_anywhere("openai-chat", "gpt-5-mini")
    assert policy.sha256 is not None and len(policy.sha256) == 64


def test_the_policy_cannot_grant_plain_http_by_itself():
    with pytest.raises(ai_policy.PolicyInvalid, match="plain HTTP"):
        ai_policy.parse(document(OLLAMA), po.PrivateOrigins.empty())


def test_a_loopback_credential_entry_does_not_open_a_model_endpoint():
    with pytest.raises(ai_policy.PolicyInvalid, match="plain HTTP"):
        ai_policy.parse(document(OLLAMA), origins(credentials="loopback"))


def test_served_models_need_an_operator_controlled_origin():
    served_cloud = {**OPENAI, "models": "served"}
    with pytest.raises(ai_policy.PolicyInvalid, match="every installed model"):
        ai_policy.parse(document(served_cloud), origins())


@pytest.mark.parametrize(
    "change",
    [
        {"credential": "required"},
        {"url": "http://metadata.google.internal/v1"},
        {"url": "https://user:secret@api.example/v1"},
        {"models": ["gpt-5", "gpt-5"]},
        {"models": ["*"]},
        {"providers": ["ollama"]},
        {"compat": "vllm"},
        {"structuredOutput": "json"},
        {"caBundle": "relative/ca.pem"},
        {"contextTokens": 100},
        {"unknown": True},
        {"pairs": [["openai-chat", "other"]]},
    ],
)
def test_invalid_endpoints_are_refused(change):
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.parse(document({**OLLAMA, **change}), origins())


@pytest.mark.parametrize(
    "url",
    ["https://169.254.169.254/v1", "https://[::ffff:169.254.169.254]/v1", "https://[2002:a9fe:a9fe::1]/v1"],
)
def test_always_refused_address_literals_are_never_model_endpoints(url):
    with pytest.raises(ai_policy.PolicyInvalid, match="endpoints/0"):
        ai_policy.parse(document({**OPENAI, "url": url}))
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.from_pairs({("openai-chat", "fixture")}, [url])
    assert ai_policy.parse(document({**OPENAI, "url": "https://[2606:4700::1111]/v1"})).endpoints


def test_duplicate_keys_large_files_and_other_versions_are_refused():
    with pytest.raises(ai_policy.PolicyInvalid, match="strict JSON"):
        ai_policy.parse(b'{"version": 2, "version": 2, "endpoints": []}')
    with pytest.raises(ai_policy.PolicyInvalid, match="64 KiB"):
        ai_policy.parse(b" " * (ai_policy.MAX_FILE_BYTES + 1))
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.parse(json.dumps({"version": 3, "endpoints": []}).encode())


def test_version_one_keeps_its_exact_pairs_and_warns(caplog):
    legacy = {"models": [{"provider": "openai-chat", "model": "gpt-4o"}], "endpoints": ["https://api.openai.com/v1"]}
    with caplog.at_level(logging.WARNING, logger="weave.ai_policy"):
        policy = ai_policy.parse(json.dumps(legacy).encode())
    assert "ai_policy.legacy" in caplog.text
    entry = policy.entry_for("https://api.openai.com/v1")
    assert policy.version == 1 and entry is not None and entry.credential == "required"
    assert entry.approves("openai-chat", "gpt-4o") and not entry.approves("openai-chat", "gpt-5")
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.parse(json.dumps({**legacy, "endpoints": ["http://ollama:11434/v1"]}).encode(), origins())
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.parse(json.dumps({**legacy, "endpoints": ["https://metadata/v1"]}).encode())


def test_a_repeated_version_one_endpoint_is_listed_once():
    url = "https://api.openai.com/v1"
    legacy = {"models": [{"provider": "openai-chat", "model": "gpt-4o"}], "endpoints": [url, url]}
    assert [entry.url for entry in ai_policy.parse(json.dumps(legacy).encode()).endpoints] == [url]
    assert [entry.url for entry in ai_policy.from_pairs([("openai-chat", "fixture")], [url, url]).endpoints] == [url]


def test_unparsable_urls_and_deep_nesting_are_policy_errors():
    legacy = {"models": [{"provider": "openai-chat", "model": "gpt-4o"}], "endpoints": ["https://[bad/v1"]}
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.parse(json.dumps(legacy).encode())
    with pytest.raises(ai_policy.PolicyInvalid):
        ai_policy.from_pairs([("openai-chat", "fixture")], ["https://[bad/v1"])
    with pytest.raises(ai_policy.PolicyInvalid, match="strict JSON"):
        ai_policy.parse(b"[" * 60000)


def test_in_code_pairs_behave_like_version_one():
    policy = ai_policy.from_pairs({("openai-chat", "fixture")}, {"https://API.openai.com:443/v1"})
    assert policy.entry_for("https://API.openai.com:443/v1") is not None
    assert policy.approves_anywhere("openai-chat", "fixture")
    assert not policy.approves_anywhere("openai-chat", "other")


def test_render_writes_canonical_version_two_bytes():
    data = ai_policy.render([OLLAMA])
    assert json.loads(data) == {"version": 2, "endpoints": [OLLAMA]}
    assert data == ai_policy.render([dict(reversed(list(OLLAMA.items())))])
    assert data.endswith(b"\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_policy_file_reloads_on_change_and_fails_closed(tmp_path):
    path = tmp_path / "ai-policy.json"
    path.write_bytes(ai_policy.render([OLLAMA]))
    path.chmod(0o444)
    source = ai_policy.PolicyFile(path, origins())
    assert source.current().entry_for("http://ollama:11434/v1").served
    path.chmod(0o644)
    path.write_text("{ not json")
    os.utime(path, ns=(1, 1))
    with pytest.raises(ai_policy.PolicyInvalid):
        source.current()
    path.write_bytes(ai_policy.render([{**OLLAMA, "models": ["qwen3:4b"]}]))
    os.utime(path, ns=(2, 2))
    assert source.current().entry_for("http://ollama:11434/v1").models == ("qwen3:4b",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_reload_returns_its_own_policy_while_another_caller_fails(tmp_path, monkeypatch):
    path = tmp_path / "ai-policy.json"
    path.write_bytes(ai_policy.render([OLLAMA]))
    path.chmod(0o644)
    source = ai_policy.PolicyFile(path, origins())
    emit, raced = ai_policy._emit, []

    def racing_emit(level, record):
        emit(level, record)
        if not raced:
            # Another caller sees a broken edit before this reload returns.
            raced.append(True)
            path.write_text("{ not json")
            os.utime(path, ns=(3, 3))
            with pytest.raises(ai_policy.PolicyInvalid):
                source.current()

    path.write_bytes(ai_policy.render([{**OLLAMA, "models": ["qwen3:4b"]}]))
    os.utime(path, ns=(2, 2))
    monkeypatch.setattr(ai_policy, "_emit", racing_emit)
    policy = source.current()
    assert raced and policy is not None
    assert policy.entry_for("http://ollama:11434/v1").models == ("qwen3:4b",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_policy_file_must_not_be_writable_by_others_or_a_link(tmp_path):
    path = tmp_path / "ai-policy.json"
    path.write_bytes(ai_policy.render([OLLAMA]))
    path.chmod(0o666)
    with pytest.raises(ai_policy.PolicyInvalid, match="writable"):
        ai_policy.read_file(path)
    path.chmod(0o444)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ai_policy.PolicyInvalid, match="symbolic link"):
        ai_policy.read_file(link)
    with pytest.raises(ai_policy.PolicyInvalid, match="regular file"):
        ai_policy.read_file(tmp_path)
    with pytest.raises(ai_policy.PolicyInvalid, match="missing or unreadable"):
        ai_policy.read_file(tmp_path / "missing.json")
