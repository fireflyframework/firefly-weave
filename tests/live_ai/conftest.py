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

"""Real-Ollama journeys on a local Docker platform: nothing here is mocked.

Set WEAVE_LIVE_AI_DIRECTORY to an installation created with weave platform up. Without it
every test here is skipped with that reason; with it, any missing precondition fails. The
journeys run weave platform ai enable in container mode themselves, which pulls
WEAVE_LIVE_AI_MODEL (qwen2.5:1.5b by default) into the installation's own Ollama volume.
Command output is parsed in memory; platform commands print no secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import TASK_TYPE
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_ai
from firefly_weave.sdk.client import WeaveClient

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/guides/ai-workers.md"
DIRECTORY = "WEAVE_LIVE_AI_DIRECTORY"
MODEL = "WEAVE_LIVE_AI_MODEL"
DEFAULT_MODEL = "qwen2.5:1.5b"
INPUT = {"text": "Firefly Weave runs durable workflows and records every step, so a run can be replayed."}
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
RUN_SECONDS = 600.0
HEALTHY_SECONDS = 300.0
EXAMPLE = re.compile(
    r"```yaml\n(apiVersion: weave/v1alpha1\nkind: Workflow\nmetadata:\n  name: summarize-request\n.*?)```", re.DOTALL
)


def summarize_workflow(model: str) -> dict[str, Any]:
    """The AI workers guide's summarize example with an Ollama profile; each model gets its own workflow name."""
    found = EXAMPLE.search(GUIDE.read_text(encoding="utf-8"))
    assert found is not None, "The AI workers guide no longer shows the summarize-request example."
    document = yaml.safe_load(found.group(1))
    document["metadata"]["name"] = "summarize-request-" + hashlib.sha256(model.encode()).hexdigest()[:12]
    document["spec"]["llmProfiles"]["summarizer"].update(
        model=model,
        options={"max_tokens": 256, "temperature": 0, "seed": 42},
        maxCalls=2,
        timeoutSeconds=300,
    )
    return dict(document)


def _key(kind: str, value: str) -> str:
    return f"weave-live-ai-{kind}-" + hashlib.sha256(value.encode()).hexdigest()[:40]


def _output(event: Any) -> dict[str, Any]:
    data = event.data if isinstance(event.data, dict) else {}
    output = data.get("output")
    return output if isinstance(output, dict) else {}


class Live:
    """One local platform with AI: its commands, its own Ollama container and its public API."""

    def __init__(self, directory: Path, model: str) -> None:
        self.directory, self.model = directory, model

    def receipt(self) -> dict[str, Any]:
        value = platform_ai.receipt(self.directory)
        assert value is not None and value["stage"] == "ready", "AI is not enabled on this platform."
        return value

    def weave(self, *args: str, timeout: float = 3600) -> dict[str, Any]:
        """Run one weave platform command with JSON output; fail with its output when it exits non-zero."""
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("WEAVE_", "PYFLY_"))}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "firefly_weave.cli.main",
                "platform",
                "--directory",
                str(self.directory),
                *args,
                "--output",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"weave platform {' '.join(args)} exited {result.returncode}: "
                f"{result.stdout[-2000:]} {result.stderr[-2000:]}"
            )
        return dict(json.loads(result.stdout))

    def docker(self, *args: str) -> str:
        """Run one docker command against this installation's own context."""
        context = local._load(self.directory)["context"]
        result = subprocess.run(
            ["docker", "--context", context, *args], capture_output=True, text=True, timeout=300, check=False
        )
        if result.returncode != 0:
            raise AssertionError(f"docker {args[0]} exited {result.returncode}: {result.stderr[-1000:]}")
        return result.stdout

    def ollama_containers(self) -> list[str]:
        """The installation's own Weave-managed Ollama container, found by its Compose labels."""
        project = "weave-local-" + local._load(self.directory)["id"]
        return self.docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=ollama",
        ).split()

    def wait_healthy(self, containers: list[str]) -> None:
        deadline = time.monotonic() + HEALTHY_SECONDS
        while time.monotonic() < deadline:
            states = self.docker("inspect", "--format", "{{.State.Health.Status}}", *containers).split()
            if states and all(state == "healthy" for state in states):
                return
            time.sleep(2)
        raise AssertionError("The Ollama container did not become healthy again within five minutes.")

    @asynccontextmanager
    async def client(self) -> AsyncIterator[tuple[WeaveClient, dict[str, str]]]:
        """The public API with the host token, scoped to the installation's demo workspace."""
        state, scope = local._workspace(self.directory, "that runs AI tasks")
        api = local._ready_api(self.directory, state)
        token = local._host_token(state)
        async with WeaveClient(api, lambda: token, Scope.model_validate(scope), timeout=120) as client:
            yield client, scope

    async def run(self, model: str) -> dict[str, Any]:
        """Publish and activate the summarize workflow for this model, run it once and report how it ended."""
        receipt = self.receipt()
        source = json.dumps(summarize_workflow(model), sort_keys=True, separators=(",", ":"))
        async with self.client() as (client, scope):
            published = await client.publish("workflows", source, "json", idempotency_key=_key("workflow", source))
            request = {
                "version_id": str(published.id),
                "artifact_digest": published.digest,
                "scope": scope,
                "connection_revision_ids": {"ai-provider": receipt["connection_revision_id"]},
                "worker_release_ids": {TASK_TYPE: receipt["release_id"]},
            }
            pins = f"{published.id}:{receipt['release_id']}:{receipt['connection_revision_id']}"
            activation = await client.activate(
                ActivationRequest.model_validate_json(json.dumps(request)), idempotency_key=_key("activation", pins)
            )
            run = await client.start_run(
                StartRunRequest.model_validate_json(json.dumps({"activation_id": str(activation.id), "input": INPUT})),
                idempotency_key=str(uuid4()),
            )
            deadline = time.monotonic() + RUN_SECONDS
            while True:
                current = await client.read_run(run.id)
                events = (await client.history(run.id, limit=100)).events
                code = next((_output(event).get("code") for event in events if event.type == "task_failed"), None)
                if current.state.status in TERMINAL or code is not None:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"Run {run.id} did not finish within {RUN_SECONDS:.0f} seconds.")
                await asyncio.sleep(2)
            completed = next((_output(event) for event in events if event.type == "task_completed"), {})
            replay = await client.replay(run.id) if current.state.status == "succeeded" else None
        return {
            "run_id": str(run.id),
            "status": current.state.status,
            "output": current.state.output,
            "code": code,
            "usage": completed.get("usage") or {},
            "replay": None if replay is None else replay.status,
        }


@pytest.fixture(scope="module")
def live() -> Live:
    value = os.environ.get(DIRECTORY)
    if not value:
        pytest.skip(f"Real-Ollama journeys run only with {DIRECTORY} set to a local platform directory.")
    directory = Path(value).expanduser().resolve()
    if not (directory / "platform.json").is_file():
        pytest.fail(f"{DIRECTORY} must name a Docker platform installation created with weave platform up.")
    return Live(directory, os.environ.get(MODEL) or DEFAULT_MODEL)
