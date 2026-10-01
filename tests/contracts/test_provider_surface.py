# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Provider administration remains reachable through every public surface."""

from click.testing import CliRunner


def test_provider_schema_and_cli_surface():
    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.schema_export import contract_models
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.sdk.client import WeaveClient

    assert {
        "provider-source-request",
        "provider-source",
        "provider-event",
        "provider-receipt",
        "provider-ingress-response",
    } <= contract_models().keys()
    for family, names in {
        "provider_sources": ("create", "read", "list", "disable"),
        "provider_receipts": ("read", "list", "retry"),
    }.items():
        for name in names:
            assert f"{family}.{name}" in OPERATIONS
        result = CliRunner().invoke(cli, [family.replace("_", "-"), "--help"])
        assert result.exit_code == 0
    assert callable(WeaveClient.create_provider_source)
    assert callable(WeaveClient.retry_provider_receipt)


def test_cli_receipt_identity_and_permission_exit_parity(monkeypatch):
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    import httpx

    import firefly_weave.sdk.client as client_module
    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.providers import ProviderReceipt
    from firefly_weave.sdk.client import WeaveClient

    receipt = ProviderReceipt(
        id=uuid4(),
        source_id=uuid4(),
        provider="fixture",
        event_id="e1",
        kind="message",
        fingerprint="a" * 64,
        received_at=datetime.now(UTC),
        state="blocked",
        reason="authority_unavailable",
    )
    responses = [
        httpx.Response(200, json=receipt.model_dump(mode="json")),
        httpx.Response(
            403, json={"status": 403, "code": "WV-FORBIDDEN", "message": "Access denied", "diagnostics": []}
        ),
        httpx.Response(
            500, json={"status": 500, "code": "WV-INTERNAL", "message": "Request failed", "diagnostics": []}
        ),
    ]
    paths = []

    def receive(request):
        paths.append(request.url.path)
        return responses.pop(0)

    def factory(*args, **kwargs):
        return WeaveClient(*args, **kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(client_module, "WeaveClient", factory)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "fixture")
    options = [
        "--base-url",
        "http://localhost",
        "--tenant",
        str(uuid4()),
        "--project",
        str(uuid4()),
        "--environment",
        str(uuid4()),
    ]
    runner = CliRunner()
    result = runner.invoke(cli, ["provider-receipts", "read", str(receipt.id), *options])
    assert result.exit_code == 0 and json.loads(result.output)["id"] == str(receipt.id)
    for code in (1, 3):
        result = runner.invoke(cli, ["provider-receipts", "retry", str(receipt.id), *options])
        assert result.exit_code == code
    assert paths[0].endswith("/provider-receipts/" + str(receipt.id))


def test_provider_openapi_covers_entire_ack_status_range():
    from firefly_weave.contracts.surface import OPERATIONS

    for name in ("provider_ingress.receive", "provider_ingress.challenge"):
        assert "2XX" in OPERATIONS[name].native().responses
