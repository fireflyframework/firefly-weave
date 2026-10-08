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

"""Ollama facts, the probe that runs on the platform network, and pulls from the host."""

import io
import json
import socket
from urllib.error import URLError

import httpx
import pytest

from firefly_weave import ollama

QWEN_TAG = {"name": "qwen2.5:1.5b", "size": 986061892, "details": {"family": "qwen2"}}
QWEN_SHOW = {
    "capabilities": ["completion", "tools"],
    "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768},
    "parameters": 'stop                           "<|im_end|>"',
}
GEMMA_SHOW = {
    "capabilities": ["completion"],
    "model_info": {"general.architecture": "gemma3", "gemma3.context_length": 32768},
    "parameters": "num_ctx                        2048\ntemperature                    1",
}


def test_facts_read_size_family_context_and_tools():
    assert ollama.facts(QWEN_TAG, QWEN_SHOW) == ollama.ServedModel(
        name="qwen2.5:1.5b", size_bytes=986061892, family="qwen2", context_tokens=32768, tools="yes"
    )
    gemma = ollama.facts({"name": "gemma3:270m", "size": 291}, GEMMA_SHOW)
    assert gemma.tools == "no" and gemma.context_tokens == 2048 and gemma.family is None
    assert ollama.facts({"name": "x:1b"}, None).tools == "unknown"


class Response:
    def __init__(self, body, status=200):
        self.status, self.body = status, json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return io.BytesIO(self.body).read(limit)


class Opener:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def open(self, request, timeout):
        path = request.full_url.split("11434", 1)[1]
        self.seen.append((request.get_method(), path))
        if path not in self.routes:
            raise URLError("refused")
        value = self.routes[path]
        return Response(value(request) if callable(value) else value)


def test_probe_reports_version_and_bounded_served_models():
    tags = {"models": [{"name": f"m{index:02d}:1b", "size": index} for index in range(60)]}
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": tags, "/api/show": QWEN_SHOW})
    found = ollama.probe("http://ollama:11434", opener=opener)
    assert found["version"] == "0.12.3" and len(found["models"]) == ollama.MAX_MODELS
    assert found["models"][0]["name"] == "m00:1b" and found["models"][0]["tools"] == "yes"
    assert ("POST", "/api/pull") not in opener.seen


def test_probe_of_a_server_that_does_not_answer_reports_no_version():
    assert ollama.probe("http://ollama:11434", opener=Opener({})) == {"version": None, "models": []}


def test_the_container_probe_prints_one_marked_line(monkeypatch, capsys):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.246.21.7", 11434))])
    served = ollama.facts(QWEN_TAG, QWEN_SHOW).model_dump(mode="json")
    monkeypatch.setattr(ollama, "probe", lambda origin: {"version": "0.12.3", "models": [served]})
    assert ollama.main(["http://ollama:11434"]) == 0
    output = capsys.readouterr().out.encode()
    found = ollama.parse_probe(output)
    assert found["addresses"] == ["10.246.21.7"] and found["version"] == "0.12.3"
    assert found["models"] == [ollama.facts(QWEN_TAG, QWEN_SHOW)]
    with pytest.raises(ValueError):
        ollama.parse_probe(b"no report here")
    with pytest.raises(ValueError):
        ollama.parse_probe(output + output)


def test_an_unresolvable_origin_is_not_probed(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(ollama, "probe", lambda origin: pytest.fail("must not probe"))
    ollama.main(["http://ollama:11434"])
    assert ollama.parse_probe(capsys.readouterr().out.encode()) == {"addresses": [], "version": None, "models": []}


def stream(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def test_pull_reports_progress_and_requires_success():
    seen, messages = [], []

    def handle(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            content=stream(
                {"status": "pulling manifest"},
                {"status": "pulling 1a2b", "total": 1000, "completed": 500},
                {"status": "pulling 1a2b", "total": 1000, "completed": 1000},
                {"status": "success"},
            ),
        )

    ollama.pull("http://127.0.0.1:55123", "qwen2.5:1.5b", messages.append, transport=httpx.MockTransport(handle))
    assert seen == [("POST", "/api/pull", {"model": "qwen2.5:1.5b", "stream": True})]
    assert messages[-1] == "Pulling qwen2.5:1.5b: success"
    assert "Pulling qwen2.5:1.5b: pulling 1a2b 50%" in messages


@pytest.mark.parametrize(
    "body,reason",
    [
        (
            stream({"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}),
            "file does not",
        ),
        (stream({"status": "pulling manifest"}), "before Ollama reported success"),
    ],
)
def test_an_interrupted_or_refused_pull_raises_with_its_reason(body, reason):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match=reason):
        ollama.pull("http://127.0.0.1:55123", "nope:1b", lambda message: None, transport=transport)
