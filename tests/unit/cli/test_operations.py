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

"""Operator CLI wire contract and safe offline import coverage."""

import io
import json
from uuid import uuid4

from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_resolution_cli_preserves_receipt_revision_and_bearer(monkeypatch, tmp_path):
    from firefly_weave.cli import operations

    captured = []

    def send(request, timeout):
        captured.append(request)
        return io.BytesIO(b'{"revision":2}')

    monkeypatch.setattr(operations, "open_request", send)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "private-test-token")
    incident = uuid4()
    body = {"receipt_id": str(uuid4()), "kind": "terminate", "reason": "operator decision"}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(body))
    result = CliRunner().invoke(
        cli,
        [
            "incident",
            "resolve",
            str(incident),
            "--revision",
            "1",
            "--request",
            str(path),
            "--environment-url",
            "https://weave.invalid/tenants/t/projects/p/environments/e",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].full_url.endswith(f"/incidents/{incident}/resolve")
    assert captured[0].get_header("If-match") == "1"
    assert captured[0].get_header("Authorization") == "Bearer private-test-token"
    assert json.loads(captured[0].data) == body
    assert "private-test-token" not in result.output


def test_cancel_retry_and_list_cli(monkeypatch, tmp_path):
    from firefly_weave.cli import operations

    calls = []
    monkeypatch.setattr(operations, "invoke", lambda *args: calls.append(args))
    monkeypatch.setenv("WEAVE_ENVIRONMENT_URL", "https://weave.invalid/env")
    run = str(uuid4())
    path = tmp_path / "start.json"
    path.write_text(json.dumps({"activation_id": str(uuid4()), "input": 2}))
    runner = CliRunner()
    for args in (
        ["run", "cancel", run, "--reason", "stop"],
        ["run", "retry", run, "--request", str(path), "--idempotency-key", "stable"],
        ["incident", "list", run],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
    assert calls[0][1:] == (f"/runs/{run}/cancel", {"reason": "stop"})
    assert calls[1][3] == {"Idempotency-Key": "stable"}
    assert calls[2][1] == f"/runs/{run}/incidents"
