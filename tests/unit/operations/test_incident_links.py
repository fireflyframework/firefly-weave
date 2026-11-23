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

"""The incidents linked to an incident: who may see them (readers of every incident of the environment, nobody
else), which ones are shown (those whose own view is shown), and how many."""

from uuid import uuid4

import pytest

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.compiler.ir import accepted_ir_versions
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES
from firefly_weave.operations import fact_reads, incidents
from firefly_weave.runtime import admission

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def principal(*grants: Grant, active: bool = True) -> Principal:
    return Principal(id=uuid4(), kind="human", active=active, grants=grants)


def test_links_need_incident_read_across_the_environment_on_the_token_and_the_stored_principal():
    reads_every_incident = incidents.reads_every_incident
    authorization = AuthorizationService()
    everywhere = principal(Grant(role="operator", scope=SCOPE))
    project = principal(Grant(role="operator", scope=SCOPE.model_copy(update={"environment_id": None})))
    named = principal(Grant(role="operator", scope=SCOPE, resources=(str(uuid4()),)))
    elsewhere = principal(Grant(role="operator", scope=SCOPE.model_copy(update={"environment_id": uuid4()})))
    without = principal(Grant(role="deployer", scope=SCOPE))
    assert reads_every_incident(authorization, (everywhere, everywhere), SCOPE)
    assert reads_every_incident(authorization, (project, everywhere), SCOPE)
    # A grant that names incidents, another environment, a role without incident.read or a disabled principal: none.
    for reader in (named, elsewhere, without, principal(Grant(role="operator", scope=SCOPE), active=False)):
        assert not reads_every_incident(authorization, (reader, everywhere), SCOPE)
        assert not reads_every_incident(authorization, (everywhere, reader), SCOPE)


def found(**link) -> dict:
    """A linked incident as the case read finds it: a called run's undo stuck in doubt, its facts projected and its
    run's metadata available."""
    return {
        "link": {
            "id": str(uuid4()),
            "run_id": str(uuid4()),
            "code": "WV-TXN-UNDO-STUCK",
            "status": "active",
            "node_id": "add#undo",
            "static_node_id": "add",
            "instance_key": "add#undo",
            "projected": True,
            "workflow_name": "child-move",
            "workflow_version": "1.0.0",
            **link,
        },
        "run": {
            "classification_state": "available",
            "classification_policy": admission.POLICY,
            "pinned_ir_version": max(accepted_ir_versions(ADVERTISED_FEATURES)),
            "pinned_features": [],
            "policy_blocked": False,
        },
    }


def shown(item: dict) -> dict:
    return {
        "id": item["link"]["id"],
        "run_id": item["link"]["run_id"],
        "code": "WV-TXN-UNDO-STUCK",
        "status": "active",
        "node_id": "add",
        "instance_key": "add#undo",
        "workflow": {"name": "child-move", "version": "1.0.0"},
    }


# Why a linked incident's own view is withheld, so its link is too.
WITHHELD = {
    "its run's metadata is unavailable": {"run": {"classification_state": "unavailable"}},
    "its run's metadata is unsupported": {"run": {"classification_state": "unsupported"}},
    "its run predates the classification policy": {"run": {"classification_policy": "legacy"}},
    "its run is policy blocked": {"run": {"policy_blocked": True}},
    "its facts are not projected": {"link": {"projected": False}},
    "its facts name another step than the incident": {"link": {"node_id": "remove#undo"}},
}


@pytest.mark.parametrize("reason", sorted(WITHHELD))
def test_a_link_whose_own_view_is_withheld_is_left_out(reason):
    withheld, visible = found(), found()
    for part, change in WITHHELD[reason].items():
        withheld[part].update(change)
    assert fact_reads.case_links({"waits_on": [withheld, visible], "waits_found": 2, "held_by": withheld}) == {
        "waits_on": [shown(visible)]
    }
    # The same link is shown once its view is.
    assert fact_reads.case_links({"held_by": visible}) == {"held_by": shown(visible)}


@pytest.mark.parametrize(("count", "more"), [(20, False), (21, True)])
def test_waits_on_names_twenty_and_flags_only_a_case_with_more(count, more):
    # The read takes one more than twenty, to tell whether there are more.
    links = [found() for _ in range(count)]
    case = fact_reads.case_links({"waits_on": links, "waits_found": count})
    assert case["waits_on"] == [shown(item) for item in links[:20]]
    assert case.get("waits_on_more", False) is more
