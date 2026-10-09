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
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

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


ESCAPES = "a\x1b[2J\x1b]0;pwn\x07"
SERVED = ollama.facts(QWEN_TAG, QWEN_SHOW).model_dump(mode="json")


def report(**changes):
    value = {"addresses": ["10.246.21.7"], "version": "0.12.3", "models": [SERVED]} | changes
    return ollama.MARKER.encode() + json.dumps(value).encode()


@pytest.mark.parametrize("field,value", [("name", ESCAPES), ("name", "a b"), ("name", "a\n"), ("family", ESCAPES)])
def test_a_served_model_keeps_to_printable_name_characters(field, value):
    with pytest.raises(ValueError):
        ollama.ServedModel(**({"name": "x:1b"} | {field: value}))


def test_facts_drops_a_family_with_unsafe_characters_and_refuses_an_unsafe_or_overlong_name():
    assert ollama.facts({"name": "x:1b", "details": {"family": ESCAPES}}, None).family is None
    with pytest.raises(ValueError):
        ollama.facts({"name": ESCAPES}, None)
    with pytest.raises(ValueError):
        ollama.facts({"name": "n" * 201}, None)
    assert ollama.facts({"name": "n" * 200}, None).name == "n" * 200


def test_facts_ignores_a_context_that_is_not_a_sane_number():
    assert ollama.facts({"name": "x:1b"}, {"parameters": "num_ctx " + "9" * 4301}).context_tokens is None
    assert ollama.facts({"name": "x:1b"}, {"parameters": "num_ctx 0"}).context_tokens is None
    assert ollama.facts({"name": "x:1b"}, {"parameters": "num_ctx 4096"}).context_tokens == 4096


def test_the_version_is_printable_and_bounded_in_the_probe_and_in_the_report():
    long_version = "1" * 1_000_000
    opener = Opener({"/api/version": {"version": long_version}, "/api/tags": {"models": []}})
    assert len(ollama.probe("http://ollama:11434", opener=opener)["version"]) <= 64
    opener = Opener({"/api/version": {"version": ESCAPES}, "/api/tags": {"models": []}})
    cleaned = ollama.probe("http://ollama:11434", opener=opener)["version"]
    assert "\x1b" not in cleaned and "\x07" not in cleaned
    parsed = ollama.parse_probe(report(version=long_version))
    assert len(parsed["version"]) <= 64
    assert "\x1b" not in ollama.parse_probe(report(version=ESCAPES))["version"]
    assert ollama.parse_probe(report(version=12))["version"] is None


def test_the_probe_skips_models_with_unsafe_or_overlong_names_without_asking_about_them():
    shown = []

    def show(request):
        shown.append(json.loads(request.data)["model"])
        return QWEN_SHOW

    tags = {"models": [{"name": name} for name in ("ok:1b", "", ESCAPES, "n" * 201, "z:2b")]}
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": tags, "/api/show": show})
    found = ollama.probe("http://ollama:11434", opener=opener)
    assert [item["name"] for item in found["models"]] == ["ok:1b", "z:2b"]
    assert shown == ["ok:1b", "z:2b"]


def test_one_model_whose_facts_cannot_be_built_does_not_hide_the_others(monkeypatch):
    real = ollama.facts

    def fussy(tag, show):
        if tag["name"] == "bad:1b":
            raise ValueError("unusable")
        return real(tag, show)

    monkeypatch.setattr(ollama, "facts", fussy)
    tags = {"models": [{"name": "bad:1b"}, {"name": "good:1b"}]}
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": tags, "/api/show": QWEN_SHOW})
    found = ollama.probe("http://ollama:11434", opener=opener)
    assert [item["name"] for item in found["models"]] == ["good:1b"]


@pytest.mark.parametrize("models", [None, "qwen", 7, {"name": "x:1b"}, [None, 3, "x", ["x:1b"]]])
def test_a_malformed_model_list_reports_the_version_and_no_models(models):
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": {"models": models}})
    assert ollama.probe("http://ollama:11434", opener=opener) == {"version": "0.12.3", "models": []}


@pytest.mark.parametrize(
    "body",
    [b"[" * 100_000, b'{"models": ', b"[1, 2]", b"null"],
    ids=["too-deep", "cut-off", "list", "null"],
)
def test_a_malformed_answer_is_treated_as_no_answer_or_no_facts(body):
    assert ollama.probe("http://ollama:11434", opener=Opener({"/api/version": body})) == {
        "version": None,
        "models": [],
    }
    routes = {"/api/version": {"version": "0.12.3"}, "/api/tags": {"models": [{"name": "x:1b"}]}, "/api/show": body}
    found = ollama.probe("http://ollama:11434", opener=Opener(routes))
    assert found["models"] == [ollama.facts({"name": "x:1b"}, None).model_dump(mode="json")]


def test_only_http_and_https_origins_are_probed():
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": {"models": []}})
    for origin in ("file:///etc/passwd", "ftp://ollama:11434", "ollama:11434", "http://[::1", ""):
        assert ollama.probe(origin, opener=opener) == {"version": None, "models": []}
    assert opener.seen == []
    assert ollama.probe("https://ollama:11434", opener=opener)["version"] == "0.12.3"


def test_an_origin_with_a_trailing_slash_is_probed_without_a_doubled_slash():
    opener = Opener({"/api/version": {"version": "0.12.3"}, "/api/tags": {"models": []}})
    assert ollama.probe("http://ollama:11434/", opener=opener)["version"] == "0.12.3"
    assert opener.seen == [("GET", "/api/version"), ("GET", "/api/tags")]


@pytest.mark.parametrize(
    "origin",
    [
        "http://ollama:abc",
        "http://ollama:99999",
        "ftp://ollama",
        "http://[::1",
        "",
        "ollama",
        "http://",
        "http://:11434",
    ],
)
def test_an_origin_that_cannot_be_probed_still_prints_one_empty_report(monkeypatch, capsys, origin):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("must not resolve"))
    monkeypatch.setattr(ollama, "probe", lambda origin: pytest.fail("must not probe"))
    assert ollama.main([origin]) == 0
    assert ollama.parse_probe(capsys.readouterr().out.encode()) == {"addresses": [], "version": None, "models": []}


def test_the_report_keeps_at_most_the_served_model_limit_and_a_few_addresses():
    assert len(ollama.parse_probe(report(models=[SERVED] * ollama.MAX_MODELS))["models"]) == ollama.MAX_MODELS
    with pytest.raises(ValueError):
        ollama.parse_probe(report(models=[SERVED] * (ollama.MAX_MODELS + 1)))
    addresses = [f"10.246.21.{index}" for index in range(1, 17)]
    assert ollama.parse_probe(report(addresses=addresses))["addresses"] == addresses
    with pytest.raises(ValueError):
        ollama.parse_probe(report(addresses=[*addresses, "10.246.21.17"]))


@pytest.mark.parametrize(
    "line",
    [
        report(addresses=[167772161]),
        report(addresses=[None]),
        report(addresses=["not an address"]),
        report(addresses="10.0.0.1"),
        report(addresses=None),
        report(models=None),
        report(models="x"),
        report(models=[None]),
        report(models=[{"name": ESCAPES}]),
        report(models=[{"name": "x:1b", "extra": 1}]),
        ollama.MARKER.encode() + b"{}",
        ollama.MARKER.encode() + b"[]",
        ollama.MARKER.encode() + b"null",
        ollama.MARKER.encode() + b"7",
        ollama.MARKER.encode() + b"not json",
        ollama.MARKER.encode() + b"[" * 100_000,
    ],
    ids=[
        "integer-address",
        "null-address",
        "bad-address",
        "address-string",
        "no-addresses",
        "no-models",
        "model-string",
        "null-model",
        "unsafe-name",
        "extra-field",
        "empty-object",
        "list",
        "null",
        "number",
        "not-json",
        "too-deep",
    ],
)
def test_a_malformed_probe_report_is_refused_with_a_value_error(line):
    with pytest.raises(ValueError):
        ollama.parse_probe(line)


class Cut(httpx.SyncByteStream):
    def __iter__(self):
        yield stream({"status": "pulling manifest"})
        raise httpx.ReadError("connection reset")


def test_a_pull_that_cannot_reach_or_keep_the_connection_raises_a_value_error():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ValueError, match="Could not pull nope:1b: connection refused"):
        ollama.pull("http://127.0.0.1:55123", "nope:1b", lambda message: None, transport=httpx.MockTransport(refuse))
    cut = httpx.MockTransport(lambda request: httpx.Response(200, stream=Cut()))
    with pytest.raises(ValueError, match="Could not pull nope:1b: connection reset"):
        ollama.pull("http://127.0.0.1:55123", "nope:1b", lambda message: None, transport=cut)
