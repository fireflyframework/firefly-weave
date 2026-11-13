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

"""J4 from the SDK and CLI: AI agents on a local Ollama with a real model.

Ollama runs in a Weave-managed container by default; WEAVE_ACC_OLLAMA=host uses the Ollama already serving on
this computer instead.

The agents work on the orders fixture (tests/acceptance/fixtures/agents), which answers order
A-17 with a tracking code only it knows (shipped-7731) and records every call, so the journey
checks what the model did by structure: the tools it called, the reviews they waited for, the
calls that reached the fixture, and that fixture token in the answer. Every run replays
consistent, and its Agent log shows each turn with its tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/acceptance/fixtures/agents"
# The gated local model, and a model too small to call tools.
MODEL = "qwen3:4b-instruct-2507-q4_K_M"
SMALL_MODEL = "gemma3:270m"
# Where Ollama runs: "container" (the default, a Weave-managed container, as CI runs it) or "host" (the Ollama
# already serving on this computer, which keeps the model blobs off the Docker disk).
OLLAMA_MODES = ("container", "host")
ORDERS_ALIAS = "orders.acceptance.test"
ORDERS_CONNECTION = "orders-local"
REVIEWERS = "agent-reviewers"
TURN_TASK = "weave-agentic.turn"
CHILD = "notify-customer@1.0.0"
TOKEN = "shipped-7731"
SETTLED = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
# A turn on a CPU-only Ollama takes up to a minute; a run waits for a few of them.
RUN_SECONDS = 900
REVIEW_SECONDS = 600


def _orders() -> Any:
    if "orders_fixture" in sys.modules:
        return sys.modules["orders_fixture"]
    spec = importlib.util.spec_from_file_location("orders_fixture", FIXTURES / "orders_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ORDERS = _orders()


# The orders fixture and its catalog.


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def orders_control(run, path: str, method: str = "GET") -> Any:
    port = run.private_record("orders-fixture")["control_port"]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        body = response.read()
        return json.loads(body) if body else None


def orders_calls(run) -> list[tuple[str, str]]:
    return [(item["method"], item["path"]) for item in orders_control(run, "/_control/calls")["calls"]]


def start_orders_fixture(run) -> None:
    """The orders fixture as one container on the installation's egress network, answering the approved
    orders origin; its control port is published on loopback only. Named after the run, so the
    teardown audit finds it if anything leaves it behind."""
    network = run.platform_state()["private_origins"]["network"]
    port = _free_port()
    started = run.require(
        run.docker(
            *("run", "--detach", "--name", f"weave-acc-{run.run_id}-orders"),
            *("--network", network, "--network-alias", ORDERS_ALIAS),
            *("--read-only", "--user", "65532:65532", "--cap-drop", "ALL"),
            *("--security-opt", "no-new-privileges:true"),
            *("--publish", f"127.0.0.1:{port}:8081"),
            *("--volume", f"{FIXTURES}:/opt/orders:ro"),
            *("--label", f"io.getfirefly.weave.acceptance.run={run.run_id}"),
            run.config["images"]["python"],
            *("python", "-I", "/opt/orders/orders_server.py"),
        )
    )
    run.save_private("orders-fixture", {"container_id": started.stdout.strip(), "control_port": port})
    deadline = time.monotonic() + 30
    while True:
        try:
            assert orders_control(run, "/_control/calls") == {"calls": []}
            return
        except OSError:
            assert time.monotonic() < deadline, "the orders fixture did not answer within 30 s"
            time.sleep(0.5)


@pytest.fixture(scope="module")
def orders(run):
    """The orders fixture for every step of this module, started by the first step that runs and removed after
    the last one."""
    if not (run.private / "orders-fixture.json").is_file():
        start_orders_fixture(run)
    yield
    record = run.private / "orders-fixture.json"
    if record.is_file():
        # By its full ID, never by name or label.
        run.docker("rm", "--force", run.private_record("orders-fixture")["container_id"])
        record.unlink()


def _http_action(name: str, method: str, path: str, response: dict[str, Any]) -> dict[str, Any]:
    from firefly_weave.sdk.http_actions import author_http_action

    result = author_http_action(
        {
            "name": name,
            "version": "1.0.0",
            "method": method,
            "pathTemplate": path,
            "parameters": [{"name": "id", "location": "path", "type": "string", "array": False, "required": True}],
            "statuses": [200],
            "emptyStatuses": [],
            "timeoutSeconds": 10,
            "stringMaxLength": 64,
            "bodyRequired": False,
            "auth": {"kind": "none"},
            "responseSchema": response,
        }
    )
    errors = sorted({item.code for item in result.diagnostics if item.severity == "error"})
    assert result.ok and result.action is not None, f"{name} was refused: {errors}"
    return dict(result.action)


def order_actions() -> dict[str, dict[str, Any]]:
    """orders-lookup (read only) and orders-cancel (changes data, so every call waits for a review)."""
    return {
        "orders-lookup": _http_action("orders-lookup", "GET", "/orders/{id}", ORDERS.ORDER_SCHEMA),
        "orders-cancel": _http_action("orders-cancel", "POST", "/orders/{id}/cancel", ORDERS.CANCELLED_SCHEMA),
    }


# The agent workflows.

INSTRUCTIONS = (
    "You help customers with their orders. Use the tools to look up facts; never guess an order's status or "
    "tracking code. Answer briefly."
)
LOOKUP = {
    "name": "lookup_order",
    "description": "Look up one order by its ID, such as A-17, and return its status and tracking code.",
    "action": "orders-lookup@1.0.0",
    "connection": "orders",
}
CANCEL = {
    "name": "cancel_order",
    "description": "Cancel one order by its ID, such as A-17. This cannot be undone.",
    "action": "orders-cancel@1.0.0",
    "connection": "orders",
}
NOTIFY = {
    "name": "notify_customer",
    "description": "Send a short message to a customer, such as C-1001.",
    "workflow": CHILD,
}
TRACKED = {
    "type": "object",
    "properties": {"orderId": {"type": "string"}, "tracking": {"type": "string"}},
    "required": ["orderId", "tracking"],
    "additionalProperties": False,
}
ORDER_ID = {
    "type": "object",
    "properties": {"orderId": {"type": "string"}},
    "required": ["orderId"],
    "additionalProperties": False,
}
ANSWER = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
REQUEST = {
    "type": "object",
    "properties": {"request": {"type": "string", "minLength": 1, "maxLength": 500}},
    "required": ["request"],
    "additionalProperties": False,
}
REVIEW = {"assignment": REVIEWERS, "distinctReviewer": True}


def profile(result: dict[str, Any], model: str = MODEL) -> dict[str, Any]:
    return {
        "provider": "openai-chat",
        "model": model,
        "options": {"max_tokens": 1024, "temperature": 0, "seed": 42},
        "outputSchema": result,
    }


def agent(identifier: str, prompt: dict[str, Any], tools: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "agent",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": identifier,
        "connection": "ai",
        "instructions": {"literal": INSTRUCTIONS},
        "prompt": prompt,
        "tools": tools,
        **fields,
    }


def agent_workflow(
    name: str,
    steps: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    output: dict[str, Any],
    *,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema or {"type": "object", "additionalProperties": False},
            "outputSchema": {},
            "connections": {
                "ai": {"connector": "weave-agentic-provider@1.0.0"},
                "orders": {"connector": "weave-http@2.0.0"},
            },
            "llmProfiles": profiles,
            "steps": steps,
            "output": output,
        },
    }


def memory_workflow() -> dict[str, Any]:
    """Two agents that share one run memory: the second answers from what the first was asked."""
    memory = {"scope": "run", "key": "order-chat"}
    return agent_workflow(
        "agent-run-memory",
        [
            agent("lookup", {"literal": "What is the status of order A-17?"}, [LOOKUP], memory=memory),
            agent("recall", {"literal": "What order did I ask about?"}, [], memory=memory),
        ],
        {"lookup": profile(TRACKED), "recall": profile(ORDER_ID)},
        {
            "object": {
                "lookup": {"ref": "/steps/lookup/output/result"},
                "recall": {"ref": "/steps/recall/output/result"},
            }
        },
    )


def desk_workflow() -> dict[str, Any]:
    """An agent that may cancel orders: each cancellation waits for a reviewer other than the run's starter."""
    return agent_workflow(
        "agent-order-desk",
        [agent("desk", {"ref": "/input/request"}, [LOOKUP, CANCEL], review=REVIEW)],
        {"desk": profile(ANSWER)},
        {"ref": "/steps/desk/output/result"},
        input_schema=REQUEST,
    )


def notify_workflow() -> dict[str, Any]:
    """An agent whose tool is the callable notify-customer workflow, run as a reviewed child run."""
    return agent_workflow(
        "agent-notify",
        [agent("notifier", {"literal": "Tell customer C-1001 that order A-17 has shipped."}, [NOTIFY], review=REVIEW)],
        {"notifier": profile(ANSWER)},
        {"ref": "/steps/notifier/output/result"},
    )


# A context of about 900 tokens: past a 512-token context however the model counts it.
LONG_CONTEXT = " ".join(f"Order Z-{number} is archived." for number in range(1, 181))


def lookup_workflow(name: str, model: str = MODEL, **fields: Any) -> dict[str, Any]:
    return agent_workflow(
        name,
        [agent("lookup", {"literal": "What is the tracking code of order A-17?"}, [LOOKUP], **fields)],
        {"lookup": profile(TRACKED, model)},
        {"ref": "/steps/lookup/output/result"},
    )


WORKFLOWS: dict[str, Callable[[], dict[str, Any]]] = {
    "memory": memory_workflow,
    "desk": desk_workflow,
    "notify": notify_workflow,
    "lookup": lambda: lookup_workflow("agent-order-lookup"),
    "small": lambda: lookup_workflow("agent-order-lookup-small-model", SMALL_MODEL),
    "tight": lambda: lookup_workflow(
        "agent-order-lookup-small-context", context={"literal": LONG_CONTEXT}, memory={"contextTokens": 512}
    ),
}


# Runs, reviews and evidence.


def stopped(status: str, incident: str | None) -> bool:
    """A run that ended, or stopped at an incident."""
    return status in SETTLED or (status == "suspended" and bool(incident))


async def settle(
    client: Any, run_id: UUID, *, until: Callable[[str, str | None], bool] = stopped, seconds: float = RUN_SECONDS
) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        view = await client.invoke("runs.read", identifier=run_id)
        if until(view.state.status, view.state.incident):
            return view
        assert time.monotonic() < deadline, f"run {run_id} still {view.state.status} after {seconds:.0f} s"
        await asyncio.sleep(2)


async def start(client: Any, run, name: str, label: str, value: dict[str, Any] | None = None) -> UUID:
    from firefly_weave.contracts.runtime import StartRunRequest

    activation = UUID(run.private_record("agents")["activations"][name])
    started = await client.invoke(
        "runs.start",
        body=StartRunRequest(activation_id=activation, input=value or {}),
        idempotency_key=f"j4-{label}-{run.run_id}-{uuid4().hex[:12]}",
    )
    return UUID(str(started.id))


def review_of(weave: Any, run_id: UUID, seconds: float = REVIEW_SECONDS) -> dict[str, Any] | None:
    """The review that waits in this run, or None once the run ends without one."""
    deadline = time.monotonic() + seconds
    while True:
        tasks = weave("human-tasks", "list", "--limit", "100")["items"]
        found = [task for task in tasks if task["run_id"] == str(run_id) and task["status"] == "ready"]
        if found:
            return found[0]
        if weave("runs", "read", str(run_id))["state"]["status"] in SETTLED:
            return None
        assert time.monotonic() < deadline, f"no review waited in run {run_id} after {seconds:.0f} s"
        time.sleep(2)


def decide(weave: Any, task: dict[str, Any], decision: str, label: str) -> dict[str, Any]:
    claimed = weave(
        *("human-tasks", "claim", task["id"], "--idempotency-key", f"{label}-claim-{uuid4().hex}"),
        request={"expected_revision": task["revision"]},
    )
    return dict(
        weave(
            *("human-tasks", "complete", task["id"], "--idempotency-key", f"{label}-complete-{uuid4().hex}"),
            request={"expected_revision": claimed["revision"], "decision": decision, "data": {}},
        )
    )


def agent_log(weave: Any, run_id: UUID, key: str) -> dict[str, Any]:
    (activation,) = weave("runs", "agent-log", str(run_id), "--key", key)["items"]
    return dict(activation)


def calls(log: dict[str, Any], name: str | None = None) -> list[dict[str, Any]]:
    return [call for turn in log["turns"] for call in turn["calls"] if name is None or call["name"] == name]


def turn_errors(log: dict[str, Any]) -> list[str]:
    return [turn["error"]["code"] for turn in log["turns"] if turn.get("error")]


def assert_turns_counted(log: dict[str, Any]) -> None:
    """The Agent log shows every turn with its tokens, and the totals add them up."""
    assert log["turns"], log
    for turn in log["turns"]:
        assert turn["input_tokens"] > 0 and turn["output_tokens"] > 0, turn
        assert turn["started_at"] and turn["ended_at"], turn
    totals = log["totals"]
    assert totals["turns"] == len(log["turns"])
    assert totals["input_tokens"] == sum(turn["input_tokens"] for turn in log["turns"])
    assert totals["output_tokens"] == sum(turn["output_tokens"] for turn in log["turns"])


def assert_replays(weave: Any, *run_ids: UUID) -> None:
    for run_id in run_ids:
        report = weave("runs", "replay", str(run_id))
        assert report["status"] == "consistent", report


def _seconds(start: str, end: str) -> float:
    from datetime import datetime

    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 2)


def record(run, step: str, **values: Any) -> None:
    """The step's runs, turns, tokens and turn times, in evidence/agents-j4.json (IDs and counts only)."""
    path = run.evidence / "agents-j4.json"
    evidence = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    entry = dict(values)
    logs = entry.pop("logs", {})
    entry["agents"] = {
        key: {
            "status": log["status"],
            "error": (log.get("error") or {}).get("code"),
            "memory": log["memory"],
            "totals": log["totals"],
            "turns": [
                {
                    "input_tokens": turn["input_tokens"],
                    "output_tokens": turn["output_tokens"],
                    "seconds": _seconds(turn["started_at"], turn["ended_at"])
                    if turn["started_at"] and turn["ended_at"]
                    else None,
                    "calls": [(call["name"], call["status"]) for call in turn["calls"]],
                    "error": (turn.get("error") or {}).get("code"),
                }
                for turn in log["turns"]
            ],
        }
        for key, log in logs.items()
    }
    evidence[step] = entry
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ai_status(run) -> dict[str, Any]:
    return dict(json.loads(run.require(run.platform("ai", "status", "--output", "json", timeout=120)).stdout))


# J4.7: AI enabled, then the orders catalog, the reviewers and the agent workflows.


async def _identities(run) -> dict[str, str]:
    found = {}
    for name in ("builder", "approver"):
        async with run.client(name) as client:
            found[name] = str((await client.invoke("identity.read")).principal_id)
    return found


async def _orders_catalog(run) -> dict[str, Any]:
    """As the owner, through the CLI: the orders-local connection and the two orders actions, then the grant."""
    catalog = run.helper("acceptance_catalog")
    async with run.client("owner") as owner:
        version = await catalog.connector_version(owner)
    request = catalog.open_connection_request(ORDERS_CONNECTION, ORDERS.ORIGIN, version)
    async with run.cli("owner") as weave:
        connection = weave("connections", "create", request=json.loads(request.model_dump_json(by_alias=True)))
        for name, document in order_actions().items():
            weave(
                *("definitions", "publish", "--collection", "actions"),
                *("--idempotency-key", f"j4-07-{name}-{run.run_id}"),
                request={"source": json.dumps(document), "format": "json"},
            )
    run.require(
        run.platform(
            *("integrations", "grant", "--connection", connection["id"]),
            *("--access", "read", "--access", "write", "--output", "json"),
        )
    )
    return {"connection_revision_id": connection["id"], "connector_version_id": str(version)}


async def _activate_workflows(run, ai: dict[str, Any], orders: dict[str, Any], binding: str) -> dict[str, Any]:
    """As the builder, through the SDK: publish and activate every agent workflow in the demo environment."""
    from firefly_weave.contracts.catalog import ActivationRequest, PublicationRequest

    releases = {UUID(key): UUID(value) for key, value in run.status()["connector_release_ids"].items()}
    child = run.private_record("child-workflow")["activation_id"]
    activations, warnings = {}, {}
    async with run.client("builder") as client:
        for name, build in WORKFLOWS.items():
            source = json.dumps(build(), sort_keys=True)
            # Keyed by content: a step repeated in the same run publishes the same version again.
            digest = hashlib.sha256(source.encode()).hexdigest()[:16]
            published = await client.invoke(
                "definitions.publish",
                collection="workflows",
                body=PublicationRequest(source=source, format="json"),
                idempotency_key=f"j4-07-publish-{name}-{digest}-{run.run_id}",
            )
            activation = await client.invoke(
                "activations.create",
                body=ActivationRequest(
                    version_id=published.id,
                    artifact_digest=published.digest,
                    scope=client.scope,
                    connection_revision_ids={
                        "ai": UUID(ai["connection_revision_id"]),
                        "orders": UUID(orders["connection_revision_id"]),
                    },
                    connector_release_ids=releases,
                    worker_release_ids={TURN_TASK: UUID(ai["release_id"])},
                    assignment_binding_ids={REVIEWERS: UUID(binding)},
                    workflow_activation_ids={CHILD: UUID(child)} if name == "notify" else {},
                ),
                idempotency_key=f"j4-07-activate-{name}-{published.id}-{run.run_id}",
                revision=None,
            )
            activations[name] = str(activation.id)
            warnings[name] = [item.code for item in getattr(activation, "warnings", []) or []]
    return {"activations": activations, "warnings": warnings}


def ollama_mode() -> str:
    mode = os.environ.get("WEAVE_ACC_OLLAMA", "container")
    assert mode in OLLAMA_MODES, f"WEAVE_ACC_OLLAMA is {mode!r}; use one of {', '.join(OLLAMA_MODES)}"
    return mode


@pytest.mark.journey_step("J4.7")
def test_j4_07_ai_runs_on_ollama_with_the_gated_model(run, orders):
    mode = ollama_mode()
    arguments = ("ai", "enable", "--ollama", mode, "--model", MODEL, "--yes", "--output", "json")
    enabled = json.loads(run.require(run.platform(*arguments, timeout=3600)).stdout)
    assert (enabled["stage"], enabled["mode"]) == ("ready", mode), enabled
    assert enabled["test"]["ok"] is True and enabled["test"]["tool_calling"] == "supported", enabled["test"]
    status = ai_status(run)
    assert status["enabled"] is True and status["mode"] == mode, status
    if mode == "container":
        assert status["ollama"]["state"] == "running", status
    assert status["worker"]["presence"] == "recent", status["worker"]
    assert MODEL in {item["name"] for item in status["models"]["served"]}
    people = asyncio.run(_identities(run))
    orders = asyncio.run(_orders_catalog(run))

    async def reviewers() -> str:
        async with run.cli("approver") as weave:
            binding = weave(
                *("human-assignments", "put", "--idempotency-key", f"j4-07-reviewers-{run.run_id}"),
                request={"name": REVIEWERS, "principal_ids": [people["builder"], people["approver"]]},
            )
        return str(binding["binding_id"])

    binding = asyncio.run(reviewers())
    activated = asyncio.run(_activate_workflows(run, enabled, orders, binding))
    run.save_private(
        "agents",
        {**orders, "binding_id": binding, "people": people, "activations": activated["activations"]},
    )
    record(run, "J4.7", mode=mode, model=MODEL, test=enabled["test"], warnings=activated["warnings"])
    assert set(activated["activations"]) == set(WORKFLOWS)


# J4.8: run memory.


@pytest.mark.journey_step("J4.8")
def test_j4_08_two_agents_share_one_run_memory(run, orders):
    orders_control(run, "/_control/reset", "POST")

    async def body() -> tuple[UUID, Any]:
        async with run.client("builder") as client:
            run_id = await start(client, run, "memory", "08")
            return run_id, await settle(client, run_id)

    run_id, view = asyncio.run(body())

    async def logs() -> dict[str, dict[str, Any]]:
        async with run.cli("builder") as weave:
            found = {key: agent_log(weave, run_id, key) for key in ("lookup", "recall")}
            assert_replays(weave, run_id)
            return found

    found = asyncio.run(logs())
    record(run, "J4.8", run_id=str(run_id), status=view.state.status, logs=found)
    assert (view.state.status, view.state.incident) == ("succeeded", None), view.state
    output = view.state.output
    # The first agent answers from the fixture's own token; the second from what the first was asked.
    assert TOKEN in json.dumps(output["lookup"]), output
    assert "A-17" in output["recall"]["orderId"], output
    looked_up = calls(found["lookup"], "lookup_order")
    assert looked_up and all(call["status"] == "succeeded" for call in looked_up), looked_up
    assert any(TOKEN in json.dumps(call["output"]["value"]) for call in looked_up), looked_up
    assert ("GET", "/orders/A-17") in orders_calls(run)
    assert found["lookup"]["memory"] == {"scope": "run", "loaded": 0}
    assert found["recall"]["memory"]["scope"] == "run" and found["recall"]["memory"]["loaded"] >= 1
    assert calls(found["recall"]) == []
    for log in found.values():
        assert (log["status"], log["stop"]) == ("succeeded", "final"), log
        assert_turns_counted(log)


# J4.9 and J4.10: reviews.


def cancellations(run) -> list[tuple[str, str]]:
    return [call for call in orders_calls(run) if call[0] == "POST"]


Answer = Callable[[Any, dict[str, Any]], Awaitable[None]]


async def _reviewed_run(run, label: str, request: str, answer: Answer) -> UUID:
    """Start a desk run as the builder; ``answer(approver_cli, review)`` decides every review it waits for."""
    async with run.client("builder") as client:
        run_id = await start(client, run, "desk", label, {"request": request})
    async with run.cli("approver") as weave:
        while (review := review_of(weave, run_id)) is not None:
            await answer(weave, review)
    return run_id


def _rejecting(label: str) -> Answer:
    async def answer(weave: Any, review: dict[str, Any]) -> None:
        decide(weave, review, "reject", label)

    return answer


def _desk_evidence(run, run_id: UUID) -> tuple[Any, dict[str, Any]]:
    async def read() -> tuple[Any, dict[str, Any]]:
        async with run.client("builder") as client:
            view = await settle(client, run_id)
        async with run.cli("builder") as weave:
            log = agent_log(weave, run_id, "desk")
            assert_replays(weave, run_id)
        return view, log

    return asyncio.run(read())


def _injected_cancel(run) -> None:
    """A tool result that tells the model to cancel an order gains no authority: any cancellation the model
    asks for waits for a review, which the reviewer rejects, and nothing reaches the fixture."""
    orders_control(run, "/_control/reset", "POST")
    run_id = asyncio.run(_reviewed_run(run, "09-injected", "What is the status of order B-22?", _rejecting("j4-09i")))
    view, log = _desk_evidence(run, run_id)
    record(run, "J4.9/injected-cancel", run_id=str(run_id), status=view.state.status, logs={"desk": log})
    assert view.state.status == "succeeded", view.state
    assert any(call["status"] == "succeeded" for call in calls(log, "lookup_order")), log
    assert ("GET", "/orders/B-22") in orders_calls(run)
    assert all(call["status"] in {"rejected", "refused"} for call in calls(log, "cancel_order")), log
    assert cancellations(run) == []


@pytest.mark.journey_step("J4.9")
def test_j4_09_a_rejected_cancellation_never_reaches_the_orders_service(run, orders):
    orders_control(run, "/_control/reset", "POST")
    asked = []

    async def answer(weave: Any, review: dict[str, Any]) -> None:
        asked.append(review["id"])
        decide(weave, review, "reject", "j4-09")

    run_id = asyncio.run(_reviewed_run(run, "09", "Cancel order A-17.", answer))
    view, log = _desk_evidence(run, run_id)
    record(run, "J4.9", run_id=str(run_id), status=view.state.status, reviews=len(asked), logs={"desk": log})
    assert asked, "the model never asked to cancel order A-17"
    assert view.state.status == "succeeded", view.state
    cancels = calls(log, "cancel_order")
    assert cancels and cancels[0]["status"] == "rejected" and cancels[0]["review"]["decision"] == "reject", cancels
    assert all(call["status"] in {"rejected", "refused"} for call in cancels), cancels
    assert cancellations(run) == []
    assert_turns_counted(log)
    run.check("J4.9/injected-cancel", lambda: _injected_cancel(run))


@pytest.mark.journey_step("J4.10")
def test_j4_10_only_a_reviewer_other_than_the_starter_approves_and_the_call_runs_once(run, orders):
    orders_control(run, "/_control/reset", "POST")
    refusals: list[tuple[int, str]] = []
    listed: list[bool] = []

    async def answer(approver: Any, review: dict[str, Any]) -> None:
        if not refusals:
            async with run.cli("builder") as weave:
                for args, request in (
                    (("human-tasks", "read", review["id"]), None),
                    (
                        ("human-tasks", "claim", review["id"], "--idempotency-key", f"j4-10-{uuid4().hex}"),
                        {"expected_revision": review["revision"]},
                    ),
                    (
                        ("human-tasks", "complete", review["id"], "--idempotency-key", f"j4-10-{uuid4().hex}"),
                        {"expected_revision": review["revision"], "decision": "approve", "data": {}},
                    ),
                ):
                    refused = weave.refused(*args, request=request)
                    refusals.append((refused["status"], refused["code"]))
                tasks = weave("human-tasks", "list", "--limit", "100")["items"]
                listed.append(review["id"] in {task["id"] for task in tasks})
        decide(approver, review, "approve", "j4-10")

    run_id = asyncio.run(_reviewed_run(run, "10", "Cancel order A-17.", answer))
    view, log = _desk_evidence(run, run_id)
    record(run, "J4.10", run_id=str(run_id), status=view.state.status, logs={"desk": log})
    # The run's starter can't read, claim or approve its review, and doesn't see it in their list.
    assert refusals == [(403, "WV-REVIEW-DISTINCT")] * 3, refusals
    assert listed == [False]
    assert view.state.status == "succeeded", view.state
    approved = [call for call in calls(log, "cancel_order") if call["status"] == "succeeded"]
    assert len(approved) == 1 and approved[0]["review"]["decision"] == "approve", calls(log, "cancel_order")
    assert cancellations(run) == [("POST", "/orders/A-17/cancel")]
    assert_turns_counted(log)


# J4.11: a workflow as a tool.


@pytest.mark.journey_step("J4.11")
def test_j4_11_an_approved_workflow_tool_runs_a_child_run_whose_output_is_the_tool_result(run, orders):
    async def body() -> tuple[UUID, Any, dict[str, Any], Any]:
        async with run.client("builder") as client:
            run_id = await start(client, run, "notify", "11")
        async with run.cli("approver") as weave:
            review = review_of(weave, run_id)
            assert review is not None, "the notify_customer call never waited for a review"
            decide(weave, review, "approve", "j4-11")
        async with run.client("builder") as client:
            view = await settle(client, run_id)
        async with run.cli("builder") as weave:
            log = agent_log(weave, run_id, "notifier")
            (call,) = [item for item in calls(log, "notify_customer") if item["status"] == "succeeded"]
            child = weave("runs", "read", call["child_run_id"])
            assert_replays(weave, run_id, UUID(call["child_run_id"]))
        return run_id, view, log, child

    run_id, view, log, child = asyncio.run(body())
    record(run, "J4.11", run_id=str(run_id), child_run_id=child["id"], status=view.state.status, logs={"notifier": log})
    assert view.state.status == "succeeded", view.state
    (call,) = [item for item in calls(log, "notify_customer") if item["status"] == "succeeded"]
    assert call["target"] == {"kind": "workflow", "reference": CHILD}, call
    assert call["review"]["decision"] == "approve"
    assert child["state"]["status"] == "succeeded", child["state"]
    assert child["state"]["output"]["delivered"] is True
    # The child run's output is what the model read as the tool's result.
    assert call["output"] == {"available": True, "value": child["state"]["output"], "omissions": []}, call
    assert_turns_counted(log)


# J4.12 to J4.14: what the model can't do, and an endpoint that goes away.


def _failed_run(run, name: str, label: str) -> tuple[UUID, Any, dict[str, Any], list[str]]:
    async def body() -> tuple[UUID, Any, dict[str, Any], list[str]]:
        async with run.client("builder") as client:
            run_id = await start(client, run, name, label)
            view = await settle(client, run_id)
            history = await client.history(run_id, limit=200)
        failures = [
            event.data["output"]["code"]
            for event in history.events
            if event.type == "task_failed" and isinstance(event.data.get("output"), dict)
        ]
        async with run.cli("builder") as weave:
            log = agent_log(weave, run_id, "lookup")
            assert_replays(weave, run_id)
        return run_id, view, log, failures

    return asyncio.run(body())


def _assert_model_failure(run, step: str, name: str, code: str) -> None:
    orders_control(run, "/_control/reset", "POST")
    run_id, view, log, failures = _failed_run(run, name, step.replace(".", "-"))
    record(run, step, run_id=str(run_id), status=view.state.status, incident=view.state.incident, logs={"lookup": log})
    # Never retried: one failed turn, an incident at the turn's key, and no tool call reached the fixture.
    assert view.state.status == "suspended" and view.state.incident, view.state
    assert failures == [code], failures
    assert turn_errors(log) == [code], log
    assert calls(log) == [] and orders_calls(run) == []


@pytest.mark.journey_step("J4.12")
def test_j4_12_a_model_without_tool_calling_fails_with_llm_no_tool_support(run, orders):
    pulled = json.loads(
        run.require(run.platform("ai", "models", "pull", SMALL_MODEL, "--yes", "--output", "json", timeout=1800)).stdout
    )
    assert SMALL_MODEL in {item["name"] for item in pulled["served"]}, pulled
    _assert_model_failure(run, "J4.12", "small", "LLM_NO_TOOL_SUPPORT")


@pytest.mark.journey_step("J4.13")
def test_j4_13_a_prompt_past_the_context_fails_with_llm_context_limit(run, orders):
    _assert_model_failure(run, "J4.13", "tight", "LLM_CONTEXT_LIMIT")


class ContainerOllama:
    """The Weave-managed Ollama container, stopped and started by its full ID."""

    def __init__(self, run) -> None:
        project = f"weave-local-{run.platform_state()['id']}"
        listed = run.require(
            run.docker(
                *("ps", "--all", "--quiet", "--no-trunc"),
                *("--filter", f"label=com.docker.compose.project={project}"),
                *("--filter", "label=com.docker.compose.service=ollama"),
            )
        ).stdout.split()
        (self.identifier,) = listed
        self.run = run

    def stop(self) -> None:
        self.run.require(self.run.docker("stop", self.identifier, timeout=120))

    def start(self) -> None:
        self.run.require(self.run.docker("start", self.identifier, timeout=120))
        _until(
            lambda: self.run.require(
                self.run.docker("inspect", "--format", "{{.State.Health.Status}}", self.identifier)
            ).stdout.strip(),
            lambda health: health == "healthy",
            300,
            "Ollama is not healthy again",
        )


class HostOllama:
    """The Ollama server on this computer: the process listening on its port is stopped, and ollama serve
    starts it again (its log in private/), as a person would restart it."""

    def __init__(self, run) -> None:
        from firefly_weave.sdk.platform_ai import HOST_OLLAMA

        self.run = run
        self.url = HOST_OLLAMA
        self.port = int(HOST_OLLAMA.rsplit(":", 1)[1])

    def answers(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(self.url + "/api/version", timeout=2):
                return True
        except OSError:
            return False

    def stop(self) -> None:
        listening = subprocess.run(
            ["lsof", "-nP", "-t", f"-iTCP:{self.port}", "-sTCP:LISTEN"], capture_output=True, text=True, check=False
        ).stdout.split()
        assert listening, f"no Ollama process listens on port {self.port}"
        for pid in listening:
            os.kill(int(pid), signal.SIGTERM)
        _until(self.answers, lambda answered: not answered, 60, "Ollama still answers")

    def start(self) -> None:
        executable = shutil.which("ollama")
        assert executable, "the ollama command is not on PATH"
        log = (self.run.private / "ollama-serve.log").open("ab")
        subprocess.Popen(
            [executable, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
        )
        _until(self.answers, bool, 60, "Ollama does not answer again")


def _until(read: Callable[[], Any], done: Callable[[Any], bool], seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} after {seconds:.0f} s"
        time.sleep(2)


@pytest.mark.journey_step("J4.14")
def test_j4_14_a_stopped_model_endpoint_fails_with_llm_unreachable_and_the_retry_succeeds(run, orders):
    from firefly_weave.contracts.operations import IncidentResolution

    orders_control(run, "/_control/reset", "POST")
    ollama = ContainerOllama(run) if ollama_mode() == "container" else HostOllama(run)
    ollama.stop()
    try:

        async def failing() -> tuple[UUID, Any]:
            async with run.client("builder") as client:
                run_id = await start(client, run, "lookup", "14")
                return run_id, await settle(client, run_id)

        run_id, stopped_view = asyncio.run(failing())
    finally:
        ollama.start()

    async def retried() -> tuple[Any, list[dict[str, Any]], Any, dict[str, Any]]:
        async with run.client("builder") as client:
            incidents = (await client.invoke("incidents.run_list", identifier=run_id)).items
            (incident,) = [item for item in incidents if item.status == "open"]
            await client.invoke(
                "incidents.resolve",
                identifier=incident.id,
                body=IncidentResolution(receipt_id=uuid4(), kind="retry_safe", reason="Ollama is running again"),
                revision=incident.revision,
            )
            view = await settle(client, run_id, until=lambda status, _: status in SETTLED)
        async with run.cli("builder") as weave:
            log = agent_log(weave, run_id, "lookup")
            assert_replays(weave, run_id)
        return incident, incidents, view, log

    incident, incidents, view, log = asyncio.run(retried())
    record(
        run,
        "J4.14",
        run_id=str(run_id),
        stopped={"status": stopped_view.state.status, "incident": stopped_view.state.incident},
        status=view.state.status,
        logs={"lookup": log},
    )
    # The endpoint was down: recovery retried the turn, then stopped at an incident; after the restart, the
    # person's retry ran the same turn again and the agent answered from the fixture.
    assert stopped_view.state.status == "suspended" and stopped_view.state.incident, stopped_view.state
    assert "LLM_UNREACHABLE" in turn_errors(log), log
    assert view.state.status == "succeeded", view.state
    assert TOKEN in json.dumps(view.state.output), view.state.output
    assert ("GET", "/orders/A-17") in orders_calls(run)
