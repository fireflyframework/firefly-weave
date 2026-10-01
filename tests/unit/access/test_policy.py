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

"""Every canonical matrix cell and scope boundary is enforced by the service port."""

from uuid import uuid4

import pytest

EXPECTED = {
    "platform_admin": {"provider.manage", "tenant.create", "grant.admin"},
    "tenant_admin": {"project.manage", "environment.manage", "grant.manage", "connection.manage"},
    "developer": {"definition.write", "definition.publish", "compile", "simulate", "catalog.read"},
    "deployer": {"release.activate", "release.retire", "connection.bind", "subscription.manage"},
    "operator": {
        "run.start",
        "run.signal",
        "run.cancel",
        "incident.read",
        "incident.resolve",
        "run.retry",
        "delivery.read",
        "delivery.retry",
    },
    "viewer": {"catalog.read", "run.read", "status.read", "delivery.read"},
    "worker": {"worker.register", "task.claim", "task.heartbeat", "task.complete", "credential.lease"},
}


def api():
    import importlib.util

    assert importlib.util.find_spec("firefly_weave.access") is not None, "B2 access service is absent"
    from firefly_weave.access.authorization import AccessDenied, AuthorizationService
    from firefly_weave.access.models import Grant, Principal
    from firefly_weave.contracts.access import Scope

    return AccessDenied, AuthorizationService(), Grant, Principal, Scope


@pytest.mark.parametrize("role", EXPECTED)
@pytest.mark.parametrize("capability", sorted(set.union(*EXPECTED.values())))
def test_complete_role_matrix(role, capability):
    Denied, service, Grant, Principal, Scope = api()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    grant_scope = None if role == "platform_admin" else scope
    principal = Principal(
        id=uuid4(), kind="worker" if role == "worker" else "application", grants=(Grant(role=role, scope=grant_scope),)
    )
    target = None if capability in EXPECTED["platform_admin"] else scope
    if capability in EXPECTED[role]:
        service.require(principal, target, capability)
    else:
        with pytest.raises(Denied):
            service.require(principal, target, capability)


def test_scope_resources_disabled_worker_and_host_boundaries():
    Denied, service, Grant, Principal, Scope = api()
    tenant, project, env = uuid4(), uuid4(), uuid4()
    scope = Scope(tenant_id=tenant, project_id=project, environment_id=env)
    principal = Principal(id=uuid4(), kind="application", grants=(Grant(role="developer", scope=scope),))
    service.require(principal, scope, "definition.write")
    for denied in [
        Scope(tenant_id=uuid4()),
        Scope(tenant_id=tenant, project_id=uuid4()),
        Scope(tenant_id=tenant, project_id=project, environment_id=uuid4()),
    ]:
        with pytest.raises(Denied):
            service.require(principal, denied, "definition.write")
    for update in [{"active": False}, {"grants": ()}, {"kind": "worker"}]:
        with pytest.raises(Denied):
            service.require(principal.model_copy(update=update), scope, "definition.write")
    constrained = principal.model_copy(update={"grants": (Grant(role="developer", scope=scope, resources=("a",)),)})
    service.require(constrained, scope, "definition.write", resource="a")
    for resource in [None, "b"]:
        with pytest.raises(Denied):
            service.require(constrained, scope, "definition.write", resource=resource)


def test_delegation_ceiling_is_scope_and_capability_based():
    Denied, service, Grant, Principal, Scope = api()
    scope = Scope(tenant_id=uuid4())
    admin = Principal(id=uuid4(), kind="human", grants=(Grant(role="tenant_admin", scope=scope),))
    service.require_delegation(
        admin, Grant(role="developer", scope=Scope(tenant_id=scope.tenant_id, project_id=uuid4()))
    )
    for grant in [Grant(role="platform_admin", scope=None), Grant(role="tenant_admin", scope=Scope(tenant_id=uuid4()))]:
        with pytest.raises(Denied):
            service.require_delegation(admin, grant)
    developer = admin.model_copy(update={"grants": (Grant(role="developer", scope=scope),)})
    with pytest.raises(Denied):
        service.require_delegation(developer, Grant(role="developer", scope=scope))


def test_environment_scope_requires_project():
    from firefly_weave.contracts.access import Scope

    with pytest.raises(ValueError):
        Scope(tenant_id=uuid4(), environment_id=uuid4())


@pytest.mark.parametrize("variant", ["inactive", "worker", "unbound", "platform_escalation", "resources"])
def test_every_delegation_rejection_emits_one_complete_event(caplog, variant):
    import json
    import logging

    from firefly_weave.access.audit import AuditContext

    Denied, service, Grant, Principal, Scope = api()
    scope = Scope(tenant_id=uuid4())
    grant = Grant(role="viewer", scope=scope)
    principal = Principal(id=uuid4(), kind="human", grants=(Grant(role="tenant_admin", scope=scope),))
    if variant == "inactive":
        principal = principal.model_copy(update={"active": False})
    elif variant == "worker":
        principal = principal.model_copy(update={"kind": "worker"})
    elif variant == "unbound":
        principal = principal.model_copy(update={"grants": ()})
    elif variant == "platform_escalation":
        grant = Grant(role="platform_admin", scope=None)
    else:
        principal = principal.model_copy(
            update={"grants": (Grant(role="tenant_admin", scope=scope, resources=("allowed",)),)}
        )
        grant = Grant(role="viewer", scope=scope, resources=("outside",))
    context = AuditContext(run_id=uuid4())
    with caplog.at_level(logging.INFO, logger="weave.authorization"), pytest.raises(Denied):
        service.require_delegation(principal, grant, context=context)
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "weave.authorization"]
    assert len(records) == 1
    assert records[0]["outcome"] == "deny" and records[0]["capability"] == "grant.manage"
    assert records[0]["correlation"] == context.model_dump(mode="json")
    assert records[0]["identity"] is None
    with pytest.raises(ValueError):
        context.run_id = uuid4()
