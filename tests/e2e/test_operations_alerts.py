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

"""Alerts on a real local Docker platform: a failure-rate rule fires, signs its webhook and resolves.

Run it with ``pytest -m real_operations`` and WEAVE_ACC_RUN_DIR set to an acceptance run that
``scripts/acceptance.py --stages prepare,up --keep`` left in place, as the other operations journeys. Without it the
test is skipped. As the owner, it allows the acceptance.test email domain and records an opt-in for one address
there. It subscribes to ``alert.transition`` through a bearer connection to the Acme fixture,
whose sink journals each delivery with its headers, and watches a workflow of its own with the rule "run failure rate
above 0.1 over 60 seconds". Three of ten runs time out: the rule fires within 90 seconds, and the sink receives one
delivery signed with the subscription's secret. After ten successful runs, while one more starts about every ten
seconds, it resolves within 150 seconds because the failure rate fell under its threshold, and the sink receives one
signed delivery for that too. An email channel to GreenMail, and GreenMail's copy of the firing
message, join this journey once GreenMail is part of an acceptance profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
from firefly_weave.contracts.alerts import (
    AlertRecipientConsentDraft,
    AlertRuleDraft,
    AlertSelector,
    AlertSettingsUpdate,
)
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.integration_events import SubscriptionRequest
from firefly_weave.sdk.errors import WeaveError

pytestmark = pytest.mark.real_operations

ROOT = Path(__file__).resolve().parents[2]
OWNED_CONTEXT = "colima-weave-tests"
SUFFIX = uuid4().hex[:6]
WORKFLOW = "operations-alerts-" + SUFFIX
ACTION = "operations-alerts-sleep-" + SUFFIX
SINK = "/sink/operations-alerts-" + SUFFIX
ACME_ORIGIN = "http://acme.acceptance.test:8080"
# J0.7 stores the run's webhook secret under this handle; the subscription signs with it.
SIGNING_HANDLE = "order-review-webhook"
DOMAIN = "acceptance.test"
ADDRESS = f"alerts-{SUFFIX}@{DOMAIN}"
THRESHOLD = 0.1
RUN_TIMEOUT = 10
STALL_MS = RUN_TIMEOUT * 6000
FIRE_SECONDS = 90
RESOLVE_SECONDS = 150
RUNS_SECONDS = 180
DELIVERY_SECONDS = 60
CAPACITY = {"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"}


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _journeys() -> Any:
    """The J5 helpers: the platform's API and token, the fixture worker and order-review-lite's publication."""
    _load("operations_acceptance_harness", ROOT / "tests/acceptance/conftest.py")
    return _load("operations_overview_journeys", ROOT / "tests/acceptance/test_operations_overview.py")


@pytest.fixture(scope="module")
def run() -> Any:
    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if not root:
        pytest.skip("Set WEAVE_ACC_RUN_DIR to a kept acceptance run to run the operations journeys")
    path = Path(root).resolve()
    assert (ROOT / "build").resolve() in path.parents, "the run must live under this checkout's build/"
    kept = _load("operations_acceptance_harness", ROOT / "tests/acceptance/conftest.py").Run(path)
    assert kept.context == OWNED_CONTEXT, f"the journeys use only {OWNED_CONTEXT}"
    return kept


async def call(client: Any, operation: str, **kwargs: Any) -> Any:
    """One operation; a busy platform's 429 capacity refusal is sent again, as the platform asks clients to."""
    deadline = time.monotonic() + 120
    while True:
        try:
            return await client.invoke(operation, **kwargs)
        except WeaveError as error:
            if error.status != 429 or error.code not in CAPACITY or time.monotonic() > deadline:
                # The problem's diagnostics say which field was refused and why; they hold no secret values.
                error.add_note(f"{operation}: {[str(item) for item in error.diagnostics]}")
                raise
        await asyncio.sleep(2)


async def until(read: Callable[[], Awaitable[Any]], done: Callable[[Any], bool], seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = await read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} within {seconds:.0f} s: {value}"
        await asyncio.sleep(2)


def deliveries(run: Any, secret: str, rule_id: str) -> list[dict]:
    """The rule's events the sink received, oldest first and each once, every delivery checked against the
    subscription's signature: a delivery the platform sends again (its answer was lost) is the same event."""
    events: dict[str, dict] = {}
    for entry in run.acme_control("/_control/journal")["entries"]:
        if entry["path"] != SINK:
            continue
        delivery = entry["delivery"]
        body = delivery["body"].encode()
        signed = hmac.new(secret.encode(), (delivery["timestamp"] or "").encode() + b"." + body, hashlib.sha256)
        assert hmac.compare_digest(signed.hexdigest(), delivery["signature"] or ""), "a delivery is not signed"
        document = json.loads(body)
        assert document["eventId"] == delivery["event_id"] and entry["status"] == 204, entry["status"]
        event = document["payload"]
        if event["correlation_id"] == rule_id:
            events.setdefault(document["eventId"], event)
    return list(events.values())


async def delivery_connector(owner: Any) -> str:
    """The demo project's weave-http@1.0.0 connector version, published by the owner when it isn't there yet."""
    cursor = None
    while True:
        query: dict[str, str | int] = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        page = await call(owner, "definitions.list", collection="connectors", query=query)
        for item in page.items:
            if (getattr(item, "name", None), getattr(item, "version", None)) == ("weave-http", "1.0.0"):
                return str(item.id)
        cursor = page.next_cursor
        if not cursor:
            break
    source = json.dumps(HTTP_DESCRIPTOR.manifest.value)
    published = await owner.publish("connectors", source, "json", idempotency_key="operations-alerts-http-" + SUFFIX)
    return str(published.id)


async def prepare(run: Any, owner: Any, made: dict[str, Any]) -> None:
    """As the owner: acceptance.test allowed, an opt-in for one address there, and the signed subscription.

    Each record goes into ``made`` as soon as it exists, so the journey removes it even when a later step fails.
    """
    settings = await call(owner, "alert_settings.read")
    made["settings"] = settings
    allowed = AlertSettingsUpdate(
        recipient_domains=sorted({*settings.recipient_domains, DOMAIN}),
        approval_channel_ids=settings.approval_channel_ids,
    )
    # A first save takes no revision; a later one names the revision it replaces.
    saved = await call(owner, "alert_settings.update", body=allowed, revision=settings.revision or None)
    assert DOMAIN in saved.recipient_domains, saved
    statement = "The acceptance harness's mailbox agreed to receive alert mail."
    consent = await call(
        owner,
        "alert_recipient_consents.create",
        body=AlertRecipientConsentDraft(address=ADDRESS, statement=statement),
        idempotency_key="operations-alerts-consent-" + SUFFIX,
    )
    made["consent"] = consent
    identity = await call(owner, "identity.read")
    assert (consent.address, consent.domain, consent.method) == (ADDRESS, DOMAIN, "administrator_attested")
    assert consent.created_by == identity.principal_id and consent.revoked_at is None
    listed = await call(owner, "alert_recipient_consents.list", query={"limit": 100})
    assert consent.id in {item.id for item in listed.items}
    # Event deliveries go through weave-http@1.0.0 connections, whose only secret slot is the bearer token: the
    # delivery signs with it (and sends it as its bearer token, which the sink never journals).
    request = {
        "name": "operations-alerts-" + SUFFIX,
        "connector_version_id": str(await delivery_connector(owner)),
        "config": {"baseUrl": ACME_ORIGIN, "auth": "bearer"},
        "secretRef": {"token": SIGNING_HANDLE},
        "allowed_destinations": [ACME_ORIGIN],
    }
    # From JSON, as a client sends it: the contract is strict about Python types.
    connection = await call(
        owner, "connections.create", body=ConnectionRequest.model_validate_json(json.dumps(request))
    )
    made["subscription"] = await call(
        owner,
        "subscriptions.save",
        body=SubscriptionRequest(
            name="operations-alerts-" + SUFFIX,
            connection_revision_id=connection.id,
            signing_slot="token",
            path=SINK,
            event_types=("alert.transition",),
        ),
    )


async def journey(run: Any) -> dict:
    journeys = _journeys()
    platform = journeys.Platform(run)
    _, release = journeys.fixture_worker(run)
    secret = run.canary(SIGNING_HANDLE)
    activation = await journeys.lite_activation(
        platform, release, name=WORKFLOW, action=ACTION, timeout_seconds=RUN_TIMEOUT, stall=STALL_MS
    )
    made: dict[str, Any] = {}
    try:
        async with run.client("owner") as owner:
            await prepare(run, owner, made)
            draft = AlertRuleDraft(
                name="Run failure rate above 10 % for " + WORKFLOW,
                signal="run.failure_rate",
                selector=AlertSelector(workflow=WORKFLOW),
                comparator="gt",
                threshold=THRESHOLD,
                window_seconds=60,
                severity="critical",
            )
            rule = await call(owner, "alert_rules.create", body=draft, idempotency_key="operations-alerts-" + SUFFIX)
            made["rule"] = rule
            assert rule.state.state == "ok", rule.state
        rule_id = str(rule.id)

        async with platform.client() as client:

            async def start(inputs: list[dict]) -> list[str]:
                body = {"activation_id": activation["id"]}
                return [
                    (await journeys.post(client, platform.environment + "/runs", {**body, "input": item}))["id"]
                    for item in inputs
                ]

            async def ended(runs: list[str]) -> dict[str, str]:
                async def statuses() -> dict[str, str]:
                    listed = await journeys.get(
                        client, platform.environment + "/run-summaries", workflow=WORKFLOW, limit=100
                    )
                    return {item["id"]: item["status"] for item in listed["items"] if item.get("ended_at")}

                found = await until(statuses, lambda value: set(runs) <= set(value), RUNS_SECONDS, "the runs ended")
                return {key: found[key] for key in runs}

            # Seven short runs and three that wait past their 10-second timeout, all at once.
            planted_at = time.monotonic()
            planted = await start([{"ms": 50}] * 7 + [{"ms": STALL_MS}] * 3)
            async with run.client("owner") as owner:

                async def firing() -> list:
                    page = await call(owner, "alerts.list", query={"rule_id": rule_id, "state": "firing"})
                    return list(page.items)

                alert = (await until(firing, bool, FIRE_SECONDS, "the alert fired"))[0]
                fired_after = time.monotonic() - planted_at
            first = await ended(planted)
            assert sorted(first.values()) == ["succeeded"] * 7 + ["timed_out"] * 3, first
            assert alert.value_at_start > THRESHOLD and alert.severity == "critical", alert
            fired = await until(
                lambda: asyncio.to_thread(deliveries, run, secret, rule_id),
                bool,
                DELIVERY_SECONDS,
                "the sink received the firing",
            )
            assert [event["payload"]["alert"]["state"] for event in fired] == ["firing"], fired
            assert fired[0]["type"] == "alert.transition" and fired[0]["resource_id"] == str(alert.id), fired[0]
            assert fired[0]["payload"]["alert"] == {
                "state": "firing",
                "severity": "critical",
                "signal": "run.failure_rate",
                "value": alert.value_at_start,
                "threshold": THRESHOLD,
            }

            healthy = await ended(await start([{"ms": 50}] * 10))
            assert set(healthy.values()) == {"succeeded"}, healthy
            healthy_at = time.monotonic()
            fed: list[str] = []

            async def feed() -> None:
                # A successful run about every 10 s keeps the 60 s window holding runs, so the alert resolves on a
                # failure rate under its threshold, not because the window emptied.
                while True:
                    fed.extend(await start([{"ms": 50}]))
                    await asyncio.sleep(10)

            feeder = asyncio.create_task(feed())
            try:
                async with run.client("owner") as owner:

                    async def current() -> Any:
                        return await call(owner, "alerts.read", identifier=alert.id)

                    resolved = await until(
                        current, lambda value: value.state == "resolved", RESOLVE_SECONDS, "the alert resolved"
                    )
                    resolved_after = time.monotonic() - healthy_at
            finally:
                feeder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feeder
            assert resolved.resolved_reason == "threshold", resolved
            fed_runs = await ended(fed)
            assert set(fed_runs.values()) == {"succeeded"}, fed_runs
        changes = await until(
            lambda: asyncio.to_thread(deliveries, run, secret, rule_id),
            lambda value: len(value) >= 2,
            DELIVERY_SECONDS,
            "the sink received the resolve",
        )
        assert [event["payload"]["alert"]["state"] for event in changes] == ["firing", "resolved"], changes
        return {
            "fired_after_seconds": round(fired_after, 1),
            "resolved_after_seconds": round(resolved_after, 1),
            "value_at_start": alert.value_at_start,
            "resolved_reason": resolved.resolved_reason,
            "first_runs": sorted(first.values()),
            "healthy_runs": len(healthy),
            "runs_while_resolving": len(fed_runs),
            "signed_deliveries": [event["payload"]["alert"]["state"] for event in changes],
        }
    finally:
        # Leave the kept platform as the next journey expects: no rule, subscription or opt-in of ours, and the alert
        # settings as they were. The connection stays, since connections can't be deleted; once the subscription is
        # disabled nothing uses it.
        async with run.client("owner") as owner:
            with contextlib.suppress(WeaveError):
                if "rule" in made:
                    current_rule = await call(owner, "alert_rules.read", identifier=made["rule"].id)
                    await call(owner, "alert_rules.delete", identifier=current_rule.id, revision=current_rule.revision)
            with contextlib.suppress(WeaveError):
                if "subscription" in made:
                    await call(owner, "subscriptions.disable", identifier=made["subscription"].id)
            with contextlib.suppress(WeaveError):
                if "consent" in made:
                    await call(owner, "alert_recipient_consents.revoke", identifier=made["consent"].id)
            with contextlib.suppress(WeaveError):
                if "settings" in made:
                    before = made["settings"]
                    now = await call(owner, "alert_settings.read")
                    restored = AlertSettingsUpdate(
                        recipient_domains=before.recipient_domains, approval_channel_ids=now.approval_channel_ids
                    )
                    await call(owner, "alert_settings.update", body=restored, revision=now.revision)


def test_a_failure_rate_alert_fires_signs_its_webhook_and_resolves(run):
    figures = asyncio.run(journey(run))
    assert figures["fired_after_seconds"] <= FIRE_SECONDS and figures["resolved_after_seconds"] <= RESOLVE_SECONDS
    evidence = run.evidence / "operations-alerts.json"
    evidence.write_text(json.dumps(figures, indent=2) + "\n", encoding="utf-8")
