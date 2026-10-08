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

"""Ollama facts shared by the CLI, the AI gateway and the platform's in-network probe.

The probe uses only the standard library and this package's own dependencies, so it also runs
inside the server image on the platform network:
``python -I -m firefly_weave.ollama http://ollama:11434`` prints one marked JSON line with the
resolved addresses, the version and the served models. Everything it reports comes from the
network, so names, versions and counts are bounded and restricted to printable characters on
both sides of that line. Models are pulled from the host CLI only; the worker and the AI
gateway never pull.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import httpx

DEFAULT_MODEL = "qwen3:4b"
CI_MODEL = "qwen2.5:1.5b"
NO_TOOLS_MODEL = "gemma3:270m"
# Approximate Q4 download sizes shown before a pull; Ollama reports exact sizes while pulling.
KNOWN_SIZES = {DEFAULT_MODEL: 2_500_000_000, CI_MODEL: 1_000_000_000, NO_TOOLS_MODEL: 300_000_000}
MAX_MODELS = 50
MAX_ADDRESSES = 16
MAX_VERSION = 64
PROBE_SECONDS = 10.0
MAX_BODY = 4 * 1024 * 1024
MARKER = "WEAVE-OLLAMA-PROBE "
_SCHEMES = ("http", "https")
_TOKEN_CHARS = r"[A-Za-z0-9._:/@+-]+"
_TOKEN = re.compile(_TOKEN_CHARS)
# Nine digits keep ``int()`` far from Python's digit limit and above any real context length.
_NUM_CTX = re.compile(r"^num_ctx\s+(\d{1,9})\s*$", re.M)
_PRINTABLE = re.compile(r"[^\x20-\x7e]")


class ServedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200, pattern=f"^{_TOKEN_CHARS}$")
    size_bytes: int | None = Field(default=None, ge=0)
    family: str | None = Field(default=None, max_length=100, pattern=f"^{_TOKEN_CHARS}$")
    context_tokens: int | None = Field(default=None, ge=1)
    tools: Literal["yes", "no", "unknown"] = "unknown"


def _token(value: object, limit: int) -> str | None:
    """The value when it is a non-empty string of at most ``limit`` name characters, else None."""
    return value if isinstance(value, str) and 0 < len(value) <= limit and _TOKEN.fullmatch(value) else None


def _version(value: object) -> str | None:
    """A reported version as short printable text, or None when there is none."""
    if not isinstance(value, str):
        return None
    return _PRINTABLE.sub(" ", value)[:MAX_VERSION].strip() or None


def facts(tag: Mapping[str, Any], show: Mapping[str, Any] | None) -> ServedModel:
    """One served model from its ``/api/tags`` entry and, when available, its ``/api/show`` answer.

    Raises ValueError when the entry has no usable name; an unusable family is left out.
    """
    details = tag.get("details")
    family = _token(details.get("family") if isinstance(details, Mapping) else None, 100)
    size = tag.get("size")
    context: int | None = None
    tools: Literal["yes", "no", "unknown"] = "unknown"
    if show is not None:
        capabilities = show.get("capabilities")
        if isinstance(capabilities, list):
            tools = "yes" if "tools" in capabilities else "no"
        info = show.get("model_info")
        if isinstance(info, Mapping):
            architecture = info.get("general.architecture")
            length = info.get(f"{architecture}.context_length") if isinstance(architecture, str) else None
            if isinstance(length, int) and length > 0:
                context = length
        parameters = show.get("parameters")
        if isinstance(parameters, str) and (match := _NUM_CTX.search(parameters)):
            configured = int(match[1])
            if configured > 0:
                context = min(context, configured) if context else configured
    return ServedModel(
        name=tag.get("name", ""),
        size_bytes=size if isinstance(size, int) and size >= 0 else None,
        family=family,
        context_tokens=context,
        tools=tools,
    )


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}), _NoRedirects())


def _call(opener: OpenerDirector, url: str, body: Mapping[str, Any] | None, timeout: float) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if body is None else {"Content-Type": "application/json"}
    request = Request(url, data=data, headers=headers, method="GET" if body is None else "POST")
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Unexpected Ollama status")
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError("Ollama response too large")
    try:
        return json.loads(raw)
    except RecursionError as error:
        raise ValueError("Ollama response nested too deeply") from error


def probe(origin: str, *, opener: OpenerDirector | None = None, budget: float = PROBE_SECONDS) -> dict[str, Any]:
    """Version and served models of an Ollama origin; ``version`` is None when it does not answer."""
    try:
        scheme = urlsplit(origin).scheme
    except ValueError:
        scheme = ""
    if scheme not in _SCHEMES:
        return {"version": None, "models": []}
    base = origin.rstrip("/")
    client = opener or _opener()
    deadline = time.monotonic() + budget
    try:
        version = _call(client, base + "/api/version", None, 5.0).get("version")
        listed = _call(client, base + "/api/tags", None, 5.0).get("models")
    except (OSError, ValueError, AttributeError):
        return {"version": None, "models": []}
    entries = [
        item
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict) and _token(item.get("name"), 200)
    ]
    models = []
    for tag in sorted(entries, key=lambda item: item["name"])[:MAX_MODELS]:
        show = None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                answer = _call(client, base + "/api/show", {"model": tag["name"]}, min(5.0, remaining))
                show = answer if isinstance(answer, dict) else None
            except (OSError, ValueError):
                show = None
        try:
            models.append(facts(tag, show).model_dump(mode="json"))
        except ValueError:
            continue
    return {"version": _version(version), "models": models}


def main(argv: Sequence[str]) -> int:
    """Probe one origin from inside a container and print one marked JSON line (never response bodies)."""
    if len(argv) != 1:
        return 2
    try:
        parsed = urlsplit(argv[0])
        if parsed.scheme not in _SCHEMES or not parsed.hostname:
            raise ValueError("Only http and https origins with a host are probed")
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        addresses = sorted({str(info[4][0]) for info in infos})
    except (OSError, ValueError):
        addresses = []
    found = probe(argv[0]) if addresses else {"version": None, "models": []}
    print(MARKER + json.dumps({"addresses": addresses, **found}, sort_keys=True, separators=(",", ":")))
    return 0


def parse_probe(output: bytes) -> dict[str, Any]:
    """The one probe report in a container's output, validated and bounded; raise ValueError otherwise."""
    lines = [line for line in output.decode(errors="replace").splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise ValueError("One probe report is required")
    try:
        value = json.loads(lines[0][len(MARKER) :])
    except RecursionError as error:
        raise ValueError("The probe report is nested too deeply") from error
    if not isinstance(value, dict):
        raise ValueError("The probe report must be an object")
    addresses, models = value.get("addresses"), value.get("models")
    if not isinstance(addresses, list) or len(addresses) > MAX_ADDRESSES:
        raise ValueError("The probe report needs a short list of addresses")
    if not all(isinstance(item, str) for item in addresses):
        raise ValueError("Probe addresses must be strings")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise ValueError("The probe report needs a bounded list of models")
    return {
        "addresses": [str(ipaddress.ip_address(item)) for item in addresses],
        "version": _version(value.get("version")),
        "models": [ServedModel.model_validate(item) for item in models],
    }


def pull(
    base: str,
    name: str,
    progress: Callable[[str], None],
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 1800.0,
) -> None:
    """Pull one model through the host's Ollama API, reporting progress; raise ValueError on failure."""
    import httpx

    last = ""
    try:
        with (
            httpx.Client(
                base_url=base,
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout, connect=10.0),
            ) as client,
            client.stream("POST", "/api/pull", json={"model": name, "stream": True}) as response,
        ):
            if response.status_code != 200:
                raise ValueError(f"Ollama refused to pull {name} (HTTP {response.status_code})")
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                if "error" in event:
                    reason = _PRINTABLE.sub(" ", str(event["error"]))[:200]
                    raise ValueError(f"Ollama could not pull {name}: {reason}")
                status = _PRINTABLE.sub(" ", str(event.get("status", "")))[:100]
                total, completed = event.get("total"), event.get("completed")
                # Whole tens of percent keep a long download to a few lines of progress.
                share = (
                    f" {completed * 100 // total // 10 * 10}%"
                    if isinstance(total, int) and total > 0 and isinstance(completed, int)
                    else ""
                )
                message = f"Pulling {name}: {status}{share}"
                if message != last:
                    progress(message)
                    last = message
    except httpx.HTTPError as error:
        raise ValueError(f"Could not pull {name}: {_PRINTABLE.sub(' ', str(error))[:200]}") from error
    if last != f"Pulling {name}: success":
        raise ValueError(f"The pull of {name} ended before Ollama reported success")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
