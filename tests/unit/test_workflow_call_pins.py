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


"""Activation pins for workflow calls, and the side effect a run can cause through them."""

from uuid import UUID, uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest, WorkflowCallPin
from firefly_weave.definitions.calls import PinnedCallee, call_pins, call_tree_side_effect, side_effect
from firefly_weave.definitions.models import CatalogError

TENANT, PROJECT, ENVIRONMENT = (UUID(int=index) for index in (1, 2, 3))
SCOPE = Scope(tenant_id=TENANT, project_id=PROJECT, environment_id=ENVIRONMENT)
DEFINITION, ARTIFACT = "d" * 64, "a" * 64
REFERENCE = "notify-customer@1.0.0"


def caller_executable(*references):
    return {"dependencies": [{"kind": "Workflow", "reference": r, "digest": DEFINITION} for r in references]}


def callee(*, name="notify-customer", version="1.0.0", callable_field=None, call_depth=0, scope=SCOPE, **row):
    activation = Activation(
        id=uuid4(),
        revision=2,
        name=name,
        request=ActivationRequest(version_id=uuid4(), artifact_digest=ARTIFACT, scope=scope),
        retired=row.pop("activation_retired", False),
        call_depth=call_depth,
    )
    spec = {} if callable_field is None else {"callable": callable_field}
    version_row = {
        "kind": "Workflow",
        "name": name,
        "version": version,
        "retired": False,
        "digest": ARTIFACT,
        "definition_digest": DEFINITION,
        "document": {"spec": spec},
        **row,
    }
    return PinnedCallee(activation, row.pop("latest_retired", False), version_row)


async def pins_for(pinned, *references, supplied=None, max_call_depth=8):
    """Pin ``pinned`` for every called reference unless ``supplied`` says otherwise."""
    references = references or (REFERENCE,)
    supplied = {r: pinned.activation.id for r in references} if supplied is None else supplied

    async def load(identifier):
        return pinned if identifier == pinned.activation.id else None

    return await call_pins(
        "order-intake", SCOPE, caller_executable(*references), supplied, load, max_call_depth=max_call_depth
    )


async def refused(pinned, **options):
    with pytest.raises(CatalogError) as error:
        await pins_for(pinned, **options)
    assert (error.value.status, error.value.code) == (422, "WV-READINESS-CALL")
    return error.value.message


async def test_a_valid_pin_records_the_exact_callee_activation_version_and_digests():
    pinned = callee(callable_field={}, call_depth=2)
    pins, depth = await pins_for(pinned)
    assert pins == [
        WorkflowCallPin(
            reference=REFERENCE,
            activation_id=pinned.activation.id,
            activation_revision=2,
            version_id=pinned.activation.request.version_id,
            artifact_digest=ARTIFACT,
            definition_digest=DEFINITION,
        )
    ]
    assert depth == 3


async def test_a_workflow_without_calls_needs_no_pins_and_takes_no_extra_ones():
    async def load(identifier):
        raise AssertionError("nothing to load")

    assert await call_pins("order-intake", SCOPE, caller_executable(), {}, load) == ([], 0)
    with pytest.raises(CatalogError, match="Pin exactly one activation"):
        await call_pins("order-intake", SCOPE, caller_executable(), {REFERENCE: uuid4()}, load)


async def test_missing_or_extra_pins_are_refused_before_anything_is_loaded():
    pinned = callee(callable_field={})
    assert "Pin exactly one activation" in await refused(pinned, supplied={})
    extra = {REFERENCE: pinned.activation.id, "other@1.0.0": pinned.activation.id}
    assert "Pin exactly one activation" in await refused(pinned, supplied=extra)


@pytest.mark.parametrize(
    ("pinned", "message"),
    [
        pytest.param(
            callee(callable_field={}, scope=Scope(tenant_id=TENANT, project_id=PROJECT, environment_id=uuid4())),
            "not found in this environment",
            id="other-environment",
        ),
        pytest.param(callee(name="invoice", callable_field={}), "does not run the called workflow", id="other-name"),
        pytest.param(
            callee(version="2.0.0", callable_field={}), "does not run the called workflow", id="other-version"
        ),
        pytest.param(callee(callable_field={}, kind="Action"), "does not run the called workflow", id="not-a-workflow"),
        pytest.param(callee(callable_field={}, activation_retired=True), "retired", id="retired-activation"),
        pytest.param(callee(callable_field={}, latest_retired=True), "retired", id="retired-name"),
        pytest.param(callee(callable_field={}, retired=True), "retired", id="retired-version"),
        pytest.param(callee(callable_field={}, digest="b" * 64), "does not match its published", id="artifact"),
        pytest.param(callee(callable_field={}, definition_digest="c" * 64), "compiled against", id="other-definition"),
        pytest.param(callee(), "does not allow calls from this workflow", id="not-callable"),
        pytest.param(
            callee(callable_field={"allowedCallers": ["invoice-run"]}), "does not allow calls", id="other-callers"
        ),
        pytest.param(callee(callable_field={}, call_depth=8), "deeper than 8", id="too-deep"),
    ],
)
async def test_a_pin_is_refused_unless_it_is_a_live_callable_activation_of_the_exact_version(pinned, message):
    assert message in await refused(pinned)


async def test_a_pin_to_an_unknown_activation_is_refused():
    pinned = callee(callable_field={})
    assert "not found in this environment" in await refused(pinned, supplied={REFERENCE: uuid4()})


async def test_a_loader_that_answers_another_activation_does_not_pin_it():
    pinned = callee(callable_field={})

    async def load(identifier):
        return pinned

    with pytest.raises(CatalogError, match="not found in this environment"):
        await call_pins("order-intake", SCOPE, caller_executable(REFERENCE), {REFERENCE: uuid4()}, load)


async def test_the_call_depth_limit_counts_the_pinned_chain():
    assert (await pins_for(callee(callable_field={}, call_depth=1), max_call_depth=2))[1] == 2
    assert "deeper than 2" in await refused(callee(callable_field={}, call_depth=2), max_call_depth=2)


def executable_with(*effects):
    return {
        "dependencies": [
            {"kind": "Action", "reference": f"a{i}@1.0.0", "document": {"spec": {"sideEffect": effect}}}
            for i, effect in enumerate(effects)
        ]
    }


def test_the_side_effect_of_a_workflow_is_the_strongest_of_its_actions():
    assert side_effect({"dependencies": []}) == "read_only"
    assert side_effect(executable_with("read_only", "idempotency_key", "idempotent")) == "idempotency_key"
    assert side_effect(executable_with("idempotent", "non_idempotent")) == "non_idempotent"


async def test_the_side_effect_of_a_call_tree_follows_every_pin_once():
    child, grandchild = uuid4(), uuid4()
    tree = {
        child: (executable_with("idempotent"), {"grand@1.0.0": grandchild}),
        grandchild: (executable_with("non_idempotent"), {"child@1.0.0": child}),
    }
    visits = []

    async def resolve(identifier):
        visits.append(identifier)
        return tree[identifier]

    assert await call_tree_side_effect(executable_with("read_only"), {"child@1.0.0": child}, resolve) == (
        "non_idempotent"
    )
    assert visits == [child, grandchild]
    assert await call_tree_side_effect(executable_with("idempotent"), {}, resolve) == "idempotent"


def test_activation_fields_for_calls_are_omitted_when_unused():
    request = ActivationRequest(version_id=uuid4(), artifact_digest=ARTIFACT, scope=SCOPE)
    activation = Activation(id=uuid4(), revision=1, name="order-intake", request=request)
    assert "workflow_activation_ids" not in request.model_dump()
    assert {"workflow_call_pins", "call_depth"}.isdisjoint(activation.model_dump())
    pinned = request.model_copy(update={"workflow_activation_ids": {REFERENCE: uuid4()}})
    assert ActivationRequest.model_validate_json(pinned.model_dump_json()) == pinned
    with pytest.raises(ValueError):
        ActivationRequest.model_validate(
            {**request.model_dump(mode="json"), "workflow_activation_ids": {"notify": str(uuid4())}}
        )
