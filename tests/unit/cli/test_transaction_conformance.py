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


"""`weave transactions conformance`: a local check of one workflow step against the developer's own handlers."""

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.definitions import load_definition

CATALOG = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text(encoding="utf-8"))
CANARY_INPUT, CANARY_OUTPUT, CANARY_SECRET = "M-canary-1c7e", "h-canary-93ab", "s-canary-50fd"

HANDLERS = """
from firefly_weave.contracts.connectors import ConnectorFailure

TOMBSTONES = {tombstones}
answers, steps, seen = {{}}, {{}}, []


def handler(capability):
    async def handle(lease):
        seen.append(lease.operation_key)
        if lease.operation_key in answers:
            return answers[lease.operation_key]
        phase, branch = lease.transaction.phase, lease.transaction.branch_key
        if phase in ("try", "forward"):
            if steps.get(branch) == "undone":
                raise ConnectorFailure("TXN_ALREADY_UNDONE", "failed", effect="none")
            steps[branch] = "applied"
            answer = {{"holdId": "{output}"}} if capability.startswith("source-system-hold") else {{"id": "a-1"}}
        else:
            if phase != "confirm" and (TOMBSTONES or branch in steps):
                steps[branch] = "undone"
            answer = {{}}
        answers[lease.operation_key] = answer
        return answer

    return handle


def handlers():
    names = {names}
    return {{name: handler(name) for name in names}}
"""
CAPABILITIES = [
    "source-system-hold-quantity@1.0.0",
    "source-system-confirm-hold@1.0.0",
    "source-system-release-hold@1.0.0",
]
SQL_ACTION = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "sql.release-hold", "version": "1.0.0"},
    "spec": {
        "implementation": {
            "kind": "connector",
            "uses": "weave-postgresql@1.0.0",
            "action": "idempotent-command",
            "config": {
                "statement": "DELETE FROM holds WHERE reference = :reference",
                "parameters": {"reference": {"type": "string"}},
            },
        },
        "connection": {"connector": "weave-postgresql@1.0.0"},
        "sideEffect": "idempotency_key",
        "timeoutSeconds": 30,
        "inputSchema": {"type": "object", "properties": {"parameters": {"type": "object"}}, "required": ["parameters"]},
        "outputSchema": {"type": "object"},
    },
}


def workflow(cancel: str = "source-system.release-hold@1.0.0") -> dict:
    release = {"object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}}
    if cancel.startswith("sql."):
        release = {"object": {"parameters": {"object": {"reference": {"ref": "/input/moveId"}}}}}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-checked", "version": "1.0.0"},
        "spec": {
            "connections": {"sql": {"connector": "weave-postgresql@1.0.0"}},
            "inputSchema": {
                "type": "object",
                "properties": {"moveId": {"type": "string"}, "quantity": {"type": "integer"}},
                "required": ["moveId", "quantity"],
            },
            "outputSchema": {},
            "steps": [
                {
                    "id": "move",
                    "kind": "transaction",
                    "steps": [
                        {
                            "id": "hold",
                            "kind": "action",
                            "uses": "source-system.hold-quantity@1.0.0",
                            "connection": "sql",
                            "with": {
                                "object": {
                                    "quantity": {"ref": "/input/quantity"},
                                    "reference": {"ref": "/input/moveId"},
                                }
                            },
                            "confirm": {
                                "kind": "action",
                                "uses": "source-system.confirm-hold@1.0.0",
                                "with": {"object": {"holdId": {"ref": "/steps/hold/output/holdId"}}},
                            },
                            "cancel": {"kind": "action", "uses": cancel, "with": release},
                        },
                        {
                            "id": "submit",
                            "kind": "action",
                            "uses": "orders.submit-order@1.0.0",
                            "with": {"object": {"reference": {"ref": "/input/moveId"}}},
                            "irreversible": True,
                        },
                    ],
                    "output": {"literal": "moved"},
                }
            ],
            "output": {"literal": "done"},
        },
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A workflow, its catalog lock, an input and a handlers module in a folder on the import path."""
    monkeypatch.syspath_prepend(str(tmp_path))
    documents = [*CATALOG["definitions"], SQL_ACTION]
    lock = {
        "definitions": [
            {
                "document": item,
                "digest": FrozenDocument.from_value(load_definition(item).model_dump(by_alias=True)).digest,
            }
            for item in documents
        ],
        "tasks": CATALOG["tasks"],
        "adapters": [],
        "schemas": {},
    }
    (tmp_path / "move.workflow.yaml").write_text(yaml.safe_dump(workflow()), encoding="utf-8")
    (tmp_path / "sql.workflow.yaml").write_text(yaml.safe_dump(workflow("sql.release-hold@1.0.0")), encoding="utf-8")
    (tmp_path / "catalog.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "input.json").write_text(json.dumps({"moveId": CANARY_INPUT, "quantity": 2}), encoding="utf-8")

    def handlers(*, tombstones: bool = True, names=tuple(CAPABILITIES)) -> str:
        module = f"participant_{uuid4().hex}"
        text = HANDLERS.format(tombstones=tombstones, output=CANARY_OUTPUT, names=list(names))
        (tmp_path / f"{module}.py").write_text(text, encoding="utf-8")
        return module

    def run(*arguments: str, workflow: str = "move.workflow.yaml", step: str = "hold", env: dict | None = None):
        base = ["transactions", "conformance", str(tmp_path / workflow), "--step", step]
        base += ["--catalog", str(tmp_path / "catalog.lock.json"), "--input", str(tmp_path / "input.json")]
        return CliRunner().invoke(cli, [*base, *arguments], env=env, catch_exceptions=False)

    return tmp_path, handlers, run


def test_every_check_passes_for_a_step_that_keeps_the_contract(project):
    _, handlers, run = project
    result = run("--handlers", f"{handlers()}:handlers")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:7] == [
        f"passed  {name}"
        for name in (
            "try-then-confirm",
            "try-then-cancel",
            "cancel-without-try",
            "try-after-cancel",
            "try-twice",
            "confirm-twice",
            "cancel-twice",
        )
    ]
    assert lines[-1] == "7 of 7 checks passed."


def test_a_step_without_tombstones_fails_the_late_try(project):
    _, handlers, run = project
    module = handlers(tombstones=False)
    result = run("--handlers", f"{module}:handlers")
    assert result.exit_code == 1
    assert "FAILED  try-after-cancel: The step was applied after its cancel or undo." in result.output.splitlines()
    assert result.output.splitlines()[-1] == "6 of 7 checks passed."
    report = json.loads(run("--handlers", f"{module}:handlers", "--output", "json").output)
    assert report["passed"] is False
    assert [item["check"] for item in report["checks"] if not item["passed"]] == ["try-after-cancel"]


def test_the_report_never_shows_inputs_outputs_or_secrets(project):
    _, handlers, run = project
    module = handlers(tombstones=False)
    secret = ["--secret", "sql.password=PARTICIPANT_SECRET"]
    env = {"PARTICIPANT_SECRET": CANARY_SECRET}
    outputs = [
        run("--handlers", f"{module}:handlers", *secret, env=env).output,
        run("--handlers", f"{module}:handlers", *secret, "--output", "json", env=env).output,
        run("--handlers", f"{module}:handlers", "--vary", "/quantity", env=env).output,
        run("--handlers", f"{module}:handlers", "--vary", f"/{CANARY_INPUT}", env=env).output,
        run("--handlers", f"{module}:handlers", "--connection", f"sql={CANARY_INPUT}", env=env).output,
    ]
    assert "try-after-cancel" in outputs[0] and "WV-CONFORMANCE-INPUT" in outputs[2]
    for output in outputs:
        for canary in (CANARY_INPUT, CANARY_OUTPUT, CANARY_SECRET):
            assert canary not in output


def test_a_step_that_isnt_undone_is_refused(project):
    _, handlers, run = project
    result = run("--handlers", f"{handlers()}:handlers", step="submit")
    assert result.exit_code == 2
    assert "WV-CONFORMANCE-STEP" in result.output
    assert "Choose a step inside a transaction whose confirm and cancel, or undo, run actions." in result.output


def test_a_missing_handler_stops_the_check_before_any_request(project):
    _, handlers, run = project
    module = handlers(names=CAPABILITIES[:2])
    result = run("--handlers", f"{module}:handlers")
    assert result.exit_code == 2
    assert "WV-CONFORMANCE-HANDLER" in result.output
    assert sys.modules[module].seen == []
    assert run().exit_code == 2 and "WV-CONFORMANCE-HANDLER" in run().output
    for target in (f"{module}:nothing", "no_such_module_here:handlers", module):
        result = run("--handlers", target)
        assert result.exit_code == 2 and ("WV-CONFORMANCE-HANDLER" in result.output or "WV-CLI-USAGE" in result.output)


def test_an_unset_secret_variable_stops_the_check(project, monkeypatch):
    tmp_path, handlers, run = project
    (tmp_path / "conn.json").write_text(
        json.dumps(
            {
                "name": "sql",
                "connector_version_id": str(uuid4()),
                "config": {"dialect": "postgresql", "host": "127.0.0.1", "port": 5432, "database": "d", "user": "u"},
                "secretRef": {"password": "sql-password"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(sys.modules, "firefly_weave.connectors.postgresql", None)
    module = handlers(names=CAPABILITIES[:2])
    connection = ["--connection", f"sql={tmp_path / 'conn.json'}"]
    for arguments in (
        [*connection, "--secret", "sql.password=UNSET_PARTICIPANT_VARIABLE"],
        connection,
        ["--secret", "sql.password=PATH"],
    ):
        result = run("--handlers", f"{module}:handlers", *arguments, workflow="sql.workflow.yaml")
        assert result.exit_code == 2
        assert "WV-CONFORMANCE-CONNECTION" in result.output
    assert sys.modules[module].seen == []


def test_a_postgresql_step_needs_the_server_extra(project, monkeypatch):
    tmp_path, handlers, run = project
    (tmp_path / "conn.json").write_text(
        json.dumps({"name": "sql", "connector_version_id": str(uuid4()), "secretRef": {"password": "sql-password"}}),
        encoding="utf-8",
    )
    monkeypatch.setitem(sys.modules, "firefly_weave.connectors.postgresql", None)
    result = run(
        "--handlers",
        f"{handlers(names=CAPABILITIES[:2])}:handlers",
        "--connection",
        f"sql={tmp_path / 'conn.json'}",
        "--secret",
        "sql.password=HOME",
        workflow="sql.workflow.yaml",
    )
    assert result.exit_code == 2
    assert "WV-CONFORMANCE-DEPENDENCY" in result.output


def test_an_unreadable_workflow_or_lock_is_a_usage_error(project):
    tmp_path, handlers, run = project
    module = handlers()
    (tmp_path / "broken.yaml").write_text("steps: [CANARY-FLOW-6e1f", encoding="utf-8")
    result = run("--handlers", f"{module}:handlers", workflow="broken.yaml")
    assert result.exit_code == 2 and "WV-CLI-SOURCE" in result.output and "CANARY-FLOW-6e1f" not in result.output
    result = run("--handlers", f"{module}:handlers", workflow="missing.yaml")
    assert result.exit_code == 2 and "WV-CLI-READ" in result.output
    (tmp_path / "catalog.lock.json").write_text('{"definitions": "CANARY-LOCK-2b9d"}', encoding="utf-8")
    result = run("--handlers", f"{module}:handlers")
    assert result.exit_code == 2
    assert "WV-CLI-CATALOG" in result.output and "CANARY-LOCK-2b9d" not in result.output


def test_help_lists_transactions_under_author_locally():
    help_text = CliRunner().invoke(cli, ["--help"]).output
    section = help_text.split("Author locally:")[1].split("\n\n")[0]
    assert "transactions" in section
    assert "Check that a step Weave undoes handles every request it can get." in " ".join(section.split())
    result = CliRunner().invoke(cli, ["help", "transactions", "conformance"])
    assert result.exit_code == 0 and "--step" in result.output
