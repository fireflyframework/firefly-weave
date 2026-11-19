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


"""The participant check: which step it checks, the requests it builds, and how it judges the answers.

The participants here are small systems written for these tests, kept in a dict; they are what the check judges.
"""

import asyncio
import copy
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import evaluate
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectorFailure
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.transactions import undo_context
from firefly_weave.contracts.workers import LeaseTransaction
from firefly_weave.sdk.participant_conformance import (
    Completed,
    ConformanceError,
    ConnectorParticipant,
    Failed,
    HandlerParticipant,
    Participants,
    Request,
    check_step,
    participant_step,
)

CATALOG = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text(encoding="utf-8"))
EXAMPLE = yaml.safe_load(Path("examples/language/move-between-systems.workflow.yaml").read_text(encoding="utf-8"))
INPUT = {"moveId": "M-1", "quantity": 2}
HOLD, CONFIRM, RELEASE = (
    "source-system-hold-quantity@1.0.0",
    "source-system-confirm-hold@1.0.0",
    "source-system-release-hold@1.0.0",
)
ADD, REMOVE = "target-system-add-quantity@1.0.0", "target-system-remove-quantity@1.0.0"


def catalog(timeouts: dict[str, int] | None = None) -> CatalogSnapshot:
    definitions = copy.deepcopy(CATALOG["definitions"])
    for document in definitions:
        if document["metadata"]["name"] in (timeouts or {}):
            document["spec"]["timeoutSeconds"] = timeouts[document["metadata"]["name"]]
    return CatalogSnapshot.from_definitions(
        [load_definition(document) for document in definitions], tasks=CATALOG["tasks"]
    )


def workflow(hold_with: dict | None = None, cancel_connection: str | None = None) -> dict:
    """A move whose hold reserves on the source slot and whose add an action undoes."""
    cancel = {
        "kind": "action",
        "uses": "source-system.release-hold@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}
        },
    }
    if cancel_connection:
        cancel["connection"] = cancel_connection
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-checked", "version": "1.0.0"},
        "spec": {
            "connections": {"source": {"connector": "weave-postgresql@1.0.0"}},
            "inputSchema": EXAMPLE["spec"]["inputSchema"],
            "outputSchema": {},
            "steps": [
                {
                    "id": "lookup",
                    "kind": "action",
                    "uses": "reports.lookup@1.0.0",
                    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
                },
                {
                    "id": "move",
                    "kind": "transaction",
                    "businessKey": {"ref": "/input/moveId"},
                    "steps": [
                        {
                            "id": "hold",
                            "kind": "action",
                            "uses": "source-system.hold-quantity@1.0.0",
                            "connection": "source",
                            "with": hold_with
                            or {
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
                            "cancel": cancel,
                        },
                        {
                            "id": "add",
                            "kind": "action",
                            "uses": "target-system.add-quantity@1.0.0",
                            "with": {
                                "object": {
                                    "quantity": {"ref": "/input/quantity"},
                                    "reference": {"ref": "/input/moveId"},
                                }
                            },
                            "compensate": {
                                "kind": "action",
                                "uses": "target-system.remove-quantity@1.0.0",
                                "with": {
                                    "object": {
                                        "reference": {"ref": "/input/moveId"},
                                        "addOperation": {"ref": "/participant/operationKey"},
                                    }
                                },
                            },
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
                },
            ],
            "output": {"literal": "done"},
        },
    }


class System:
    """A participant system kept in a dict. ``tombstones``, ``replays`` and ``empty_cancels`` turn off one part of
    the contract each; ``hang`` names a capability that never answers; ``outputs`` overrides answers."""

    def __init__(self, *, tombstones=True, replays=True, empty_cancels=True, hang=None, outputs=None, late=None):
        self.tombstones, self.replays, self.empty_cancels, self.late = tombstones, replays, empty_cancels, late
        self.hang, self.outputs = hang, outputs or {}
        self.answers: dict[str, object] = {}
        self.steps: dict[str, str] = {}
        self.leases: list = []
        self.counter = 0

    def handlers(self) -> dict:
        return {capability: self.handler(capability) for capability in (HOLD, CONFIRM, RELEASE, ADD, REMOVE)}

    def handler(self, capability: str):
        async def handle(lease):
            self.leases.append(lease)
            if capability == self.hang:
                await asyncio.Event().wait()
            if self.replays and lease.operation_key in self.answers:
                return self.answers[lease.operation_key]
            phase, branch = lease.transaction.phase, lease.transaction.branch_key
            if phase in {"try", "forward"}:
                if self.steps.get(branch) == "undone":
                    if self.late == "raises":
                        raise KeyError(branch)
                    if self.late == "handler-failed":
                        raise ConnectorFailure("HANDLER_FAILED", "failed", effect="none")
                    effect = "unknown" if self.late == "unknown" else "none"
                    raise ConnectorFailure("TXN_ALREADY_UNDONE", "failed", effect=effect)
                self.steps[branch] = "applied"
                self.counter += 1
                answer = {"holdId": f"h-{self.counter}"} if capability == HOLD else {"id": f"a-{self.counter}"}
            else:
                if phase != "confirm" and branch not in self.steps:
                    if not self.empty_cancels:
                        raise ConnectorFailure("NOT_FOUND", "failed")
                    if self.tombstones:
                        self.steps[branch] = "undone"
                elif phase != "confirm":
                    self.steps[branch] = "undone"
                answer = {}
            answer = self.outputs.get(capability, answer)
            self.answers[lease.operation_key] = answer
            return answer

        return handle


def ids(count: int = 40):
    values = iter(UUID(int=index + 1) for index in range(count))
    return lambda: next(values)


async def run(system: System, step: str = "hold", *, document: dict | None = None, snapshot=None, **options):
    checked = participant_step(document or workflow(), step, snapshot or catalog())
    return await check_step(checked, HandlerParticipant(system.handlers()), INPUT, new_id=ids(), **options)


def failures(report) -> dict[str, str]:
    return {item.check: item.reason for item in report.checks if not item.passed}


def test_a_reserve_step_names_its_try_confirm_and_cancel():
    step = participant_step(workflow(), "hold", catalog())
    assert (step.workflow, step.step, step.transaction, step.mode) == ("move-checked@1.0.0", "hold", "move", "reserve")
    assert step.forward.reference == "source-system.hold-quantity@1.0.0"
    assert step.confirm.reference == "source-system.confirm-hold@1.0.0"
    assert step.cancel.reference == "source-system.release-hold@1.0.0"
    assert step.compensate is None
    assert {item.connection for item in (step.forward, step.confirm, step.cancel)} == {"source"}
    assert participant_step(workflow(cancel_connection="other"), "hold", catalog()).cancel.connection == "other"


def test_steps_that_arent_undone_by_actions_are_refused():
    for document, step in (
        (workflow(), "submit"),
        (workflow(), "lookup"),
        (workflow(), "move"),
        (workflow(), "nowhere"),
        (EXAMPLE, "add"),
    ):
        with pytest.raises(ConformanceError) as refused:
            participant_step(document, step, catalog())
        assert refused.value.code == "WV-CONFORMANCE-STEP"
    missing = workflow()
    missing["spec"]["steps"][1]["steps"][0]["cancel"]["uses"] = "source-system.release-everything@1.0.0"
    with pytest.raises(ConformanceError) as refused:
        participant_step(missing, "hold", catalog())
    assert refused.value.code == "WV-CONFORMANCE-ACTION"
    assert str(refused.value) == "The catalog doesn't hold an action the step uses."


def test_a_step_inside_a_loop_or_a_branch_is_found():
    document = workflow()
    move = document["spec"]["steps"].pop(1)
    document["spec"]["steps"].append(
        {
            "id": "each",
            "kind": "forEach",
            "items": {"literal": [1]},
            "body": {
                "steps": [
                    {
                        "id": "pick",
                        "kind": "switch",
                        "cases": [{"when": {"literal": True}, "steps": [move], "output": {"literal": None}}],
                        "default": {"steps": [], "output": {"literal": None}},
                    }
                ],
                "output": {"literal": None},
            },
        }
    )
    step = participant_step(document, "hold", catalog())
    assert (step.transaction, step.mode) == ("move", "reserve")


async def test_requests_carry_weaves_keys_and_inputs():
    system = System()
    await run(system)
    first, second = UUID(int=1), UUID(int=2)
    confirm = next(lease for lease in system.leases if lease.operation_key == f"{first}:hold#confirm")
    try_ = next(lease for lease in system.leases if lease.operation_key == f"{first}:hold")
    assert confirm.input == {"holdId": system.answers[f"{first}:hold"]["holdId"]}
    assert try_.transaction == LeaseTransaction(
        id=f"{first}:move", branch_key=f"{first}:hold", phase="try", mode="reserve"
    )
    assert try_.input == {"quantity": 2, "reference": "M-1"}
    assert try_.capability == HOLD
    cancel = next(lease for lease in system.leases if lease.operation_key == f"{second}:hold#cancel")
    assert cancel.transaction == LeaseTransaction(
        id=f"{second}:move", branch_key=f"{second}:hold", phase="cancel", mode="reserve", forward_outcome="done"
    )
    step = participant_step(workflow(), "hold", catalog())
    scope = undo_context(
        run_id=str(second),
        transaction_key="move",
        business_key="M-1",
        participant_key="hold",
        outcome="done",
        input=INPUT,
        scope={"steps": {}, "output": system.answers[f"{second}:hold"]},
    )
    assert (
        cancel.input
        == evaluate(step.cancel.expression, scope)
        == {"reference": "M-1", "holdOperation": f"{second}:hold"}
    )
    empty = next(lease for lease in system.leases if lease.operation_key == f"{UUID(int=3)}:hold#cancel")
    assert empty.transaction.forward_outcome == "unknown"
    assert empty.deadline > datetime.now(UTC)


async def test_a_participant_that_keeps_the_contract_passes_every_check():
    report = await run(System())
    assert report.passed and failures(report) == {}
    assert [item.check for item in report.checks][:2] == ["try-then-confirm", "try-then-cancel"]
    late = next(item for item in report.checks if item.check == "try-after-cancel")
    assert [(item.phase, item.answer, item.code, item.effect) for item in late.deliveries] == [
        ("cancel", "completed", None, None),
        ("try", "failed", "TXN_ALREADY_UNDONE", "none"),
    ]


async def test_a_participant_without_tombstones_fails_only_the_late_try():
    report = await run(System(tombstones=False))
    assert not report.passed
    assert failures(report) == {"try-after-cancel": "applied-late"}


async def test_a_late_step_passes_only_on_the_participants_own_refusal():
    for late in ("raises", "unknown", "handler-failed"):
        assert failures(await run(System(late=late))) == {"try-after-cancel": "unrefused-late"}
        assert failures(await run(System(late=late), "add")) == {"forward-after-compensate": "unrefused-late"}
    refused = next(item for item in (await run(System())).checks if item.check == "try-after-cancel")
    assert refused.passed and (refused.deliveries[-1].code, refused.deliveries[-1].effect) == (
        "TXN_ALREADY_UNDONE",
        "none",
    )


async def test_a_participant_that_applies_a_repeat_twice_fails_the_repeat_checks():
    report = await run(System(replays=False))
    assert failures(report) == {"try-twice": "repeat"}


async def test_a_participant_that_refuses_an_empty_cancel_fails_it():
    report = await run(System(empty_cancels=False))
    assert failures(report) == {"cancel-without-try": "failed", "try-after-cancel": "failed"}
    empty = next(item for item in report.checks if item.check == "cancel-without-try")
    assert [(item.answer, item.code, item.effect) for item in empty.deliveries] == [("failed", "NOT_FOUND", "unknown")]


async def test_an_undo_step_is_checked_with_the_compensate_list():
    report = await run(System(), "add")
    assert report.mode == "compensate" and report.passed
    assert [item.check for item in report.checks] == [
        "forward-then-compensate",
        "compensate-without-forward",
        "forward-after-compensate",
        "forward-twice",
        "compensate-twice",
    ]
    assert failures(await run(System(tombstones=False), "add")) == {"forward-after-compensate": "applied-late"}


async def test_an_answer_outside_its_output_schema_fails_the_check():
    report = await run(System(outputs={HOLD: {"holdId": 5}}))
    assert failures(report)["try-then-confirm"] == "output"
    assert "cancel-without-try" not in failures(report)


async def test_a_request_past_its_time_limit_fails_the_check():
    snapshot = catalog({"source-system.confirm-hold": 1})
    report = await asyncio.wait_for(run(System(hang=CONFIRM), snapshot=snapshot), 5)
    assert failures(report) == {"try-then-confirm": "timeout", "confirm-twice": "timeout"}
    timed_out = next(item for item in report.checks if item.check == "try-then-confirm").deliveries[-1]
    assert (timed_out.phase, timed_out.answer, timed_out.code) == ("confirm", "failed", "TIMEOUT")


async def test_varied_inputs_never_repeat_a_reference_or_a_key():
    system = System()
    await run(system, vary=["/moveId"])
    by_run: dict[str, set[str]] = {}
    for lease in system.leases:
        if lease.capability == CONFIRM:
            continue
        assert re.fullmatch(r"M-1-[0-9a-f]{12}", lease.input["reference"])
        by_run.setdefault(lease.operation_key.partition(":")[0], set()).add(lease.input["reference"])
    assert all(len(references) == 1 for references in by_run.values())
    references = [next(iter(references)) for references in by_run.values()]
    assert len(set(references)) == len(references) == 7
    for pointer in ("/quantity", "/nope", "moveId"):
        with pytest.raises(ConformanceError) as refused:
            await run(System(), vary=[pointer])
        assert refused.value.code == "WV-CONFORMANCE-INPUT"


async def test_input_that_doesnt_fit_the_workflow_is_refused():
    with pytest.raises(ConformanceError) as refused:
        checked = participant_step(workflow(), "hold", catalog())
        await check_step(checked, HandlerParticipant(System().handlers()), {"moveId": "M-1"}, new_id=ids())
    assert refused.value.code == "WV-CONFORMANCE-INPUT"


async def test_a_step_that_reads_an_earlier_step_needs_its_output():
    reads = {"object": {"quantity": {"ref": "/steps/lookup/output/count"}, "reference": {"ref": "/input/moveId"}}}
    system = System()
    with pytest.raises(ConformanceError) as refused:
        await run(system, document=workflow(hold_with=reads))
    assert refused.value.code == "WV-CONFORMANCE-SCOPE"
    assert system.leases == []
    report = await run(system, document=workflow(hold_with=reads), steps={"lookup": {"count": 3}})
    assert report.passed
    assert {lease.input["quantity"] for lease in system.leases if lease.capability == HOLD} == {3}


async def test_a_handler_failure_keeps_its_code_and_effect():
    async def refuses(lease):
        raise ConnectorFailure("NOPE", "failed", effect="none")

    async def breaks(lease):
        raise KeyError("holdId")

    async def explains(lease):
        raise ConnectorFailure("hold 42 for M-1 is gone", "failed", effect="none")

    step = participant_step(workflow(), "hold", catalog())
    request = Request(
        phase="try",
        reference=step.forward.reference,
        action=step.forward.action,
        connection="source",
        input={"quantity": 2, "reference": "M-1"},
        operation_key="R:hold",
        transaction=LeaseTransaction(id="R:move", branch_key="R:hold", phase="try", mode="reserve"),
        deadline=datetime.now(UTC),
    )
    assert await HandlerParticipant({HOLD: refuses}).deliver(request) == Failed("NOPE", "failed", "none")
    assert await HandlerParticipant({HOLD: breaks}).deliver(request) == Failed("HANDLER_FAILED", "unknown", "unknown")
    # A code that isn't a failure code could carry a value, so it counts as the handler's own failure.
    assert await HandlerParticipant({HOLD: explains}).deliver(request) == Failed("HANDLER_FAILED", "unknown", "unknown")
    system = System()
    handlers = system.handlers()
    del handlers[RELEASE]
    with pytest.raises(ConformanceError) as refused:
        await check_step(step, HandlerParticipant(handlers), INPUT, new_id=ids())
    assert refused.value.code == "WV-CONFORMANCE-HANDLER"
    assert system.leases == []
    assert await HandlerParticipant(system.handlers()).deliver(request) == Completed({"holdId": "h-1"})


async def test_a_phase_input_that_doesnt_fit_its_action_is_never_sent():
    document = workflow()
    document["spec"]["steps"][1]["steps"][0]["cancel"]["with"]["object"]["reference"] = {"ref": "/input/quantity"}
    system = System()
    report = await run(system, document=document)
    assert set(failures(report).values()) == {"input"}
    assert set(failures(report)) == {"try-then-cancel", "cancel-without-try", "try-after-cancel", "cancel-twice"}
    empty = next(item for item in report.checks if item.check == "cancel-without-try").deliveries
    assert [(item.phase, item.answer, item.code, item.effect) for item in empty] == [
        ("cancel", "failed", "INVALID_INPUT", "none")
    ]
    assert not any(lease.capability == RELEASE for lease in system.leases)


SQL_SCHEMAS = {
    "inputSchema": {
        "type": "object",
        "properties": {"parameters": {"type": "object"}},
        "required": ["parameters"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"rowCount": {"type": "integer", "minimum": 0}, "rows": {"type": "array"}},
        "required": ["rowCount", "rows"],
        "additionalProperties": False,
    },
}


def sql_action(name: str, statement: str) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": "weave-postgresql@1.0.0",
                "action": "idempotent-command",
                "config": {"statement": statement, "parameters": {"reference": {"type": "string"}}},
            },
            "connection": {"connector": "weave-postgresql@1.0.0"},
            "sideEffect": "idempotency_key",
            "timeoutSeconds": 30,
            **SQL_SCHEMAS,
        },
    }


def sql_catalog() -> CatalogSnapshot:
    documents = [
        sql_action("sql.hold", "INSERT INTO holds (reference) VALUES (:reference)"),
        sql_action("sql.confirm-hold", "UPDATE holds SET kept = true WHERE reference = :reference"),
        sql_action("sql.release-hold", "DELETE FROM holds WHERE reference = :reference"),
        *CATALOG["definitions"],
    ]
    return CatalogSnapshot.from_definitions([load_definition(item) for item in documents], tasks=CATALOG["tasks"])


def sql_workflow(*, forward: str = "sql.hold", cancel_connection: str | None = None) -> dict:
    document = workflow()
    hold = document["spec"]["steps"][1]["steps"][0]
    reference = {"object": {"parameters": {"object": {"reference": {"ref": "/input/moveId"}}}}}
    hold["uses"] = f"{forward}@1.0.0"
    if forward == "sql.hold":
        hold["with"] = reference
    hold["confirm"] = {"kind": "action", "uses": "sql.confirm-hold@1.0.0", "with": reference}
    hold["cancel"] = {"kind": "action", "uses": "sql.release-hold@1.0.0", "with": reference}
    if cancel_connection:
        hold["cancel"]["connection"] = cancel_connection
    document["spec"]["connections"]["other"] = {"connector": "weave-postgresql@1.0.0"}
    return document


class Database:
    """A connector adapter written for these tests: it records each context and keeps the contract by key."""

    def __init__(self):
        self.contexts: list = []
        self.answers: dict = {}
        self.undone: set = set()
        self.secrets: list = []

    async def execute(self, input, context):
        self.contexts.append(context)
        self.secrets.append((await context.credentials("password")).value)
        scope = (context.invocation.connection.id, context.operation_key)
        branch = (context.invocation.connection.id, context.transaction.branch_key)
        if context.transaction.phase == "try" and branch in self.undone:
            raise ConnectorFailure("SQL_ALREADY_UNDONE", "failed", effect="none")
        if scope in self.answers:
            return self.answers[scope]
        if context.transaction.phase == "cancel" and branch not in self.answers:
            self.undone.add(branch)
        self.answers[scope] = {"rowCount": 1, "rows": []}
        return self.answers[scope]

    async def test_connection(self, connection):
        raise AssertionError("The check never tests a connection")


def connection(name: str, database: str) -> ConnectionRequest:
    return ConnectionRequest(
        name=name,
        connector_version_id=UUID(int=99),
        config={"database": database},
        secretRef={"password": f"{name}-password"},
    )


def connector_participant(database: Database, secrets=None, *, slots=("source", "other")) -> ConnectorParticipant:
    return ConnectorParticipant(
        {"weave-postgresql@1.0.0": database},
        {slot: connection(slot, f"{slot}-db") for slot in slots},
        secrets or (lambda slot, name: f"{slot}-secret"),
        {},
        new_id=ids(),
    )


async def test_a_connector_request_carries_the_action_config_and_the_transaction():
    database = Database()
    step = participant_step(sql_workflow(), "hold", sql_catalog())
    report = await check_step(step, connector_participant(database), INPUT, new_id=ids())
    assert report.passed
    context = database.contexts[0]
    first = UUID(int=1)
    assert context.operation_key == f"{first}:hold"
    assert context.transaction == LeaseTransaction(
        id=f"{first}:move", branch_key=f"{first}:hold", phase="try", mode="reserve"
    )
    invocation = context.invocation
    assert invocation.config == step.forward.action.spec.implementation.config
    assert invocation.action == "idempotent-command"
    assert (invocation.input_schema, invocation.output_schema) == (
        SQL_SCHEMAS["inputSchema"],
        SQL_SCHEMAS["outputSchema"],
    )
    assert (invocation.connection.adapter, invocation.connection.connector) == (
        "weave-postgresql",
        "weave-postgresql@1.0.0",
    )
    assert invocation.connection.config == {"database": "source-db"}
    assert invocation.target is None and invocation.max_request_bytes == invocation.max_response_bytes == 1048576
    await context.authorize()


async def test_every_check_uses_its_own_connection_revision():
    database = Database()
    step = participant_step(sql_workflow(), "hold", sql_catalog())
    await check_step(step, connector_participant(database), INPUT, new_id=ids())
    by_run: dict[str, set] = {}
    for context in database.contexts:
        by_run.setdefault(context.operation_key.partition(":")[0], set()).add(context.invocation.connection.id)
    assert all(len(revisions) == 1 for revisions in by_run.values())
    revisions = [next(iter(revisions)) for revisions in by_run.values()]
    assert len(set(revisions)) == len(revisions) == 7


async def test_a_connector_secret_comes_from_the_named_variable():
    database = Database()
    step = participant_step(sql_workflow(), "hold", sql_catalog())
    await check_step(step, connector_participant(database), INPUT, new_id=ids())
    assert set(database.secrets) == {"source-secret"}
    with pytest.raises(LookupError):
        await database.contexts[0].credentials("api-key")

    def missing(slot, name):
        raise KeyError(name)

    with pytest.raises(ConformanceError) as refused:
        await check_step(step, connector_participant(Database(), missing), INPUT, new_id=ids())
    assert refused.value.code == "WV-CONFORMANCE-CONNECTION"
    with pytest.raises(ConformanceError) as refused:
        await check_step(step, connector_participant(Database(), slots=("other",)), INPUT, new_id=ids())
    assert refused.value.code == "WV-CONFORMANCE-CONNECTION"


async def test_an_undo_on_another_slot_uses_that_slots_connection():
    database = Database()
    step = participant_step(sql_workflow(cancel_connection="other"), "hold", sql_catalog())
    await check_step(step, connector_participant(database), INPUT, new_id=ids())
    slots = {
        (context.transaction.phase, context.invocation.connection.config["database"]) for context in database.contexts
    }
    assert slots == {("try", "source-db"), ("confirm", "source-db"), ("cancel", "other-db")}
    assert ("other-secret" in database.secrets) and ("source-secret" in database.secrets)


async def test_requests_are_routed_by_how_their_action_runs():
    database, system = Database(), System()
    step = participant_step(sql_workflow(forward="source-system.hold-quantity"), "hold", sql_catalog())
    both = Participants(HandlerParticipant(system.handlers()), connector_participant(database))
    report = await check_step(step, both, INPUT, new_id=ids())
    # The cancel runs on another system than the step, so nothing there can refuse the late step.
    assert failures(report) == {"try-after-cancel": "applied-late"}
    assert {lease.transaction.phase for lease in system.leases} == {"try"}
    assert {context.transaction.phase for context in database.contexts} == {"confirm", "cancel"}
    for alone, code in (
        (HandlerParticipant(system.handlers()), "WV-CONFORMANCE-HANDLER"),
        (connector_participant(database), "WV-CONFORMANCE-HANDLER"),
    ):
        with pytest.raises(ConformanceError) as refused:
            await check_step(step, alone, INPUT, new_id=ids())
        assert refused.value.code == code
    with pytest.raises(ConformanceError) as refused:
        await check_step(step, Participants(HandlerParticipant(system.handlers())), INPUT, new_id=ids())
    assert refused.value.code == "WV-CONFORMANCE-CONNECTION"
