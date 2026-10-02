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

"""Provider-neutral member administration against owned PostgreSQL fixtures."""

from uuid import uuid4

import pytest
from test_human_tasks import human_identity  # noqa: F401

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.members import MemberAdminService
from firefly_weave.access.models import Grant
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.members import MemberGrantRequest, PrincipalCreateRequest, PrincipalIdentityRequest
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


async def test_principal_management_is_platform_only_and_prevents_self_lockout(access_db, provisioned):
    _, _, access, _ = access_db
    admin, scopes = provisioned
    members = MemberAdminService(access)
    created = await members.create_principal(admin, PrincipalCreateRequest(kind="human"))
    identity = PrincipalIdentityRequest(provider_id="test", issuer="https://test.invalid", subject="member")
    await members.link_identity(admin, created.id, identity)
    await members.link_identity(admin, created.id, identity)
    other = await members.create_principal(admin, PrincipalCreateRequest(kind="human"))
    with pytest.raises(CatalogError) as conflict:
        await members.link_identity(admin, other.id, identity)
    assert conflict.value.status == 409
    with pytest.raises(CatalogError):
        await members.set_active(admin, admin.id, False)
    await members.set_active(admin, created.id, False)
    page = await members.principals(admin, limit=1)
    assert len(page.items) == 1 and page.next_cursor is not None
    assert page.items[0].id != (await members.principals(admin, limit=1, cursor=page.items[0].id)).items[0].id
    viewer_id = await access.create_principal(admin, "human")
    await access.grant(admin, viewer_id, Grant(role="viewer", scope=scopes[0]))
    viewer = await access.load_principal(viewer_id)
    for method in (
        lambda: members.principals(viewer),
        lambda: members.create_principal(viewer, PrincipalCreateRequest(kind="human")),
    ):
        with pytest.raises(AccessDenied):
            await method()


async def test_members_are_tenant_scoped_current_and_delegation_bounded(access_db, provisioned):
    _, _, access, _ = access_db
    admin, scopes = provisioned
    members = MemberAdminService(access)
    tenant = Scope(tenant_id=scopes[0].tenant_id)
    other_tenant = Scope(tenant_id=scopes[1].tenant_id)
    manager_id = await access.create_principal(admin, "human")
    manager_binding = await access.grant(admin, manager_id, Grant(role="tenant_admin", scope=tenant))
    manager = await access.load_principal(manager_id)
    target = await members.create_principal(admin, PrincipalCreateRequest(kind="human"))
    body = MemberGrantRequest(principal_id=target.id, role="developer", project_id=scopes[0].project_id)
    binding = await members.grant(manager, tenant, body)
    assert (await members.grant(manager, tenant, body)).id == binding.id
    assert binding.role == "developer"
    assert all(item.scope.tenant_id == tenant.tenant_id for item in (await members.bindings(manager, tenant)).items)
    with pytest.raises(AccessDenied):
        await members.bindings(manager, other_tenant)
    with pytest.raises(CatalogError):
        await members.revoke(manager, tenant, manager_binding)
    with pytest.raises(CatalogError):
        await members.grant(manager, tenant, body.model_copy(update={"project_id": scopes[1].project_id}))
    await members.revoke(manager, tenant, binding.id)
    await members.revoke(manager, tenant, binding.id)
    await access.revoke_grant(admin, tenant, manager_binding)
    with pytest.raises(AccessDenied):
        await members.grant(manager, tenant, body)
    with pytest.raises(AccessDenied):
        await members.bindings(manager, tenant)


async def test_native_member_http_contracts_and_scoped_cursors(client, human_identity, access_db, provisioned):  # noqa: F811
    identifier, scope, headers = human_identity
    access = access_db[2]
    tenant = Scope(tenant_id=scope.tenant_id)
    url = f"/api/v1/tenants/{scope.tenant_id}/members"
    assert (await client.get(url, headers=headers)).status_code == 403
    assert (await client.post("/api/v1/admin/principals", headers=headers, json={"kind": "human"})).status_code == 403
    await access.grant(provisioned[0], identifier, Grant(role="tenant_admin", scope=tenant))
    result = await client.get(url + "?limit=1", headers=headers)
    assert result.status_code == 200 and result.json()["next_cursor"]
    cursor = result.json()["next_cursor"]
    next_page = await client.get(url, headers=headers, params={"limit": 1, "cursor": cursor})
    assert next_page.status_code == 200 and next_page.json()["items"][0]["id"] != result.json()["items"][0]["id"]
    malformed = await client.get(url, headers=headers, params={"cursor": str(uuid4())})
    assert malformed.status_code == 422
    await access.grant(provisioned[0], identifier, Grant(role="platform_admin", scope=None))
    created = await client.post("/api/v1/admin/principals", headers=headers, json={"kind": "human"})
    assert created.status_code == 201 and created.json()["active"] is True
    target = created.json()["id"]
    body = {"provider_id": "other-ciam", "issuer": "https://ciam.example.invalid/tenant", "subject": "new-member"}
    linked = await client.post(f"/api/v1/admin/principals/{target}/identity-links", headers=headers, json=body)
    assert linked.status_code == 200 and linked.json()["principal_id"] == target
    assert (
        await client.post(f"/api/v1/admin/principals/{identifier}/status", headers=headers, json={"active": False})
    ).status_code == 409
    binding = await client.post(
        url, headers=headers, json={"principal_id": target, "role": "developer", "project_id": str(scope.project_id)}
    )
    assert binding.status_code == 201 and binding.json()["role"] == "developer"
    assert (
        await client.post(url, headers=headers, json={"principal_id": target, "role": "platform_admin"})
    ).status_code == 422
    assert (await client.post(url + f"/{binding.json()['id']}/revoke", headers=headers)).json() == {"revoked": True}
    page = await client.get("/api/v1/admin/principals?limit=1", headers=headers)
    assert page.status_code == 200 and page.json()["next_cursor"]
    assert (
        await client.get(
            "/api/v1/admin/principals", headers=headers, params={"limit": 1, "cursor": page.json()["next_cursor"]}
        )
    ).status_code == 200

    from httpx import ASGITransport

    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.members import MembersClient

    async with WeaveClient(
        "http://127.0.0.1",
        lambda: headers["Authorization"].removeprefix("Bearer "),
        tenant,
        transport=ASGITransport(client._transport.app),
    ) as sdk:
        typed = MembersClient(sdk)
        assert (await typed.principals(limit=1)).items
        assert (await typed.bindings(limit=1)).items[0].scope.tenant_id == scope.tenant_id
        managed = await typed.create_principal(PrincipalCreateRequest(kind="human"))
        identity = PrincipalIdentityRequest(
            provider_id="test-sdk", issuer="https://ciam.example.invalid", subject="sdk-member"
        )
        assert (await typed.link_identity(managed.id, identity)).principal_id == managed.id
        assert (await typed.set_active(managed.id, False)).active is False
        assert (await typed.set_active(managed.id, True)).active is True
        member = await typed.grant(MemberGrantRequest(principal_id=managed.id, role="viewer"))
        assert (await typed.revoke(member.id)).revoked is True
