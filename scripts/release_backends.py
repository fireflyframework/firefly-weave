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

"""Validate explicit local release fixtures without trusting a port as ownership."""

from __future__ import annotations

import json
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path

from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "colima-weave-tests"


def run_command(argv: list[str], *, timeout: float = 30) -> bytes:
    process = None
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > 256 * 1024:
                            raise ValueError
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise ValueError
        return bytes(output)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValueError("Owned Docker inspection failed; configuration withheld") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()


def inspect_container(context: str, identifier: str) -> dict:
    command = ["docker", "--context", context]
    endpoint = json.loads(run_command(command + ["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise ValueError("A local Docker socket is required")
    template = (
        '{"id":{{json .Id}},"running":{{json .State.Running}},'
        '"labels":{{json .Config.Labels}},"ports":{{json .NetworkSettings.Ports}}}'
    )
    return json.loads(run_command(command + ["inspect", "--format", template, identifier]))


def owned_receipt() -> dict | None:
    location = os.environ.get("WEAVE_RELEASE_BACKENDS")
    if not location:
        return None
    with os.fdopen(os.open(location, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > 16384:
            raise ValueError("Release receipt must be a bounded owner-only regular file")
        receipt = json.loads(stream.read(16385))
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"version", "workspace", "context", "postgres", "keycloak"}
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
        or receipt["workspace"] != str(ROOT)
        or receipt["context"] != CONTEXT
        or os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") != CONTEXT
    ):
        raise ValueError("Release receipt does not identify this workspace and context")
    for kind, internal in (("postgres", "5432/tcp"), ("keycloak", "8080/tcp")):
        item = receipt[kind]
        if (
            not isinstance(item, dict)
            or set(item) != {"container_id", "port", "project"}
            or not isinstance(item["container_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["container_id"])
            or type(item["port"]) is not int
            or not 1024 <= item["port"] <= 65535
            or not isinstance(item["project"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", item["project"])
        ):
            raise ValueError("Invalid owned fixture identity")
        state = inspect_container(CONTEXT, item["container_id"])
        labels = state.get("labels") or {}
        mappings = (state.get("ports") or {}).get(internal) or []
        if (
            state.get("id") != item["container_id"]
            or state.get("running") is not True
            or labels.get("com.docker.compose.project") != item["project"]
            or labels.get("com.docker.compose.project.working_dir") != str(ROOT)
            or labels.get("com.docker.compose.service") != kind
            or not mappings
            or any(
                p.get("HostIp") not in {"127.0.0.1", "::1"} or p.get("HostPort") != str(item["port"]) for p in mappings
            )
        ):
            raise ValueError("Live fixture does not match the owned receipt")
    if os.environ.get("WEAVE_TEST_POSTGRES_CONTAINER") != receipt["postgres"]["container_id"]:
        raise ValueError("Restore container must match the exact owned fixture")
    return receipt


def keycloak_endpoint(legacy_ports: tuple[int, ...] = (18081,)) -> str:
    value = os.environ.get("WEAVE_KEYCLOAK_TEST_URL", "")
    receipt = owned_receipt()
    ports = (receipt["keycloak"]["port"],) if receipt else legacy_ports
    if value not in {f"http://localhost:{port}" for port in ports}:
        raise ValueError("Explicit owned Keycloak endpoint required")
    return value


def postgres_endpoint(value: str, legacy_ports: tuple[int, ...] = (55433, 55434)):
    url = make_url(value)
    receipt = owned_receipt()
    ports = (receipt["postgres"]["port"],) if receipt else legacy_ports
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.port not in ports
        or url.database != "weave_b1_control"
        or url.username != "weave_b1_owner"
        or url.query
    ):
        raise ValueError("Explicit owned PostgreSQL control database required")
    return url
