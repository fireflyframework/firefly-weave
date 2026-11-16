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

"""Alert email on real PostgreSQL: member recipients at the address they saved, outside addresses with an allowed
domain and an opt-in, settings and approval channels, channel ownership and limits, test messages, an audit without
addresses, and each person's own alert address over HTTP."""

import json
import re
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_operations_statistics import owner
from test_worker_fleet_facts import principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.alerts import (
    AlertChannelDraft,
    AlertRecipientConsentDraft,
    AlertRuleDraft,
    AlertSettingsUpdate,
    MyAlertAddressUpdate,
)
from firefly_weave.contracts.email import MailProfile
from firefly_weave.contracts.public import Revoked
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.service import EmailService, unavailable
from firefly_weave.operations.alert_channels import (
    CHANNEL_LIMIT,
    AlertAddressService,
    AlertChannelService,
    AlertRecipientConsentService,
    AlertSettingsService,
    address_hash,
    resolve_recipients,
)
from firefly_weave.operations.alert_rules import AlertRuleService

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()
PROFILE = MailProfile(
    host="127.0.0.1",
    port=25,
    tls="local_fixture",
    sender="alerts@example.com",
    allowed_recipients=("a@example.com",),
    private_networks=("127.0.0.0/8",),
)


async def add_connection(case) -> UUID:
    """A connection revision of this environment for channels to name; the sender's checks are stubbed below."""
    revision = uuid4()
    (version,) = await owner(
        case, "SELECT id FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project LIMIT 1"
    )
    await owner(
        case,
        "INSERT INTO connection_revisions(id,tenant_id,project_id,environment_id,name,revision,connector_version_id,"
        "payload) VALUES(:revision,:tenant,:project,:environment,:name,1,:version,'{}')",
        revision=revision,
        version=version["id"],
        name="mail-" + revision.hex[:8],
    )
    return revision


@pytest.fixture
async def mail(operations_case, services, provisioned, monkeypatch):
    case = operations_case
    graph = services(case.sessions)
    case.channels = graph.resolve(AlertChannelService)
    case.settings = graph.resolve(AlertSettingsService)
    case.consents = graph.resolve(AlertRecipientConsentService)
    case.addresses = graph.resolve(AlertAddressService)
    case.rules = graph.resolve(AlertRuleService)
    case.connection = await add_connection(case)
    usable = {case.connection}

    async def connection(self, tx, actor, identifier):
        # The email service's own connection checks are its tests'; here a connection either sends or doesn't, and
        # binding it needs connection.bind as it does for real.
        AuthorizationService().require(actor, tx.scope, "connection.bind")
        if identifier not in usable:
            raise unavailable()
        return PROFILE

    monkeypatch.setattr(EmailService, "connection", connection)
    administrator = ("alert_manager", "email_sender", "deployer", "tenant_admin", "viewer")
    case.admin_a = await principal_with(case, *(Grant(role=role, scope=case.scope) for role in administrator))
    case.admin_b = await principal_with(case, *(Grant(role=role, scope=case.scope) for role in administrator))
    case.ana = await principal_with(case, Grant(role="viewer", scope=case.scope))
    case.bea = await principal_with(case, Grant(role="file_reader", scope=case.scope))
    case.other = provisioned[1][1]
    return case


def draft(case, *recipients: dict[str, Any], name: str = "On call") -> AlertChannelDraft:
    return AlertChannelDraft.model_validate_json(
        json.dumps(
            {
                "kind": "email",
                "name": name,
                "connection_revision_id": str(case.connection),
                "recipients": list(recipients),
            },
            default=str,
        )
    )


async def create(case, actor: Principal, *recipients: dict[str, Any], **changes: Any):
    return await case.channels.create(
        actor, case.scope, draft(case, *recipients, **changes), str(uuid4()), context=CONTEXT
    )


async def save_address(case, person: Principal, address: str | None):
    update = MyAlertAddressUpdate(address=address)
    return await case.addresses.save_mine(person, case.scope.tenant_id, update, context=CONTEXT)


async def allow(case, actor: Principal, domains: list[str], revision: int | None = None, channels=()):
    update = AlertSettingsUpdate(recipient_domains=domains, approval_channel_ids=list(channels))
    return await case.settings.update(actor, case.scope, update, revision, context=CONTEXT)


async def opt_in(case, actor: Principal, address: str):
    request = AlertRecipientConsentDraft(address=address, statement="Agreed on the support call of 3 November")
    return await case.consents.create(actor, case.scope, request, str(uuid4()), context=CONTEXT)


def states(channel) -> list[tuple[int, str]]:
    return [(item.index, item.state) for item in channel.recipient_states]


async def test_member_recipients_get_mail_at_the_address_they_saved(mail):
    case = mail
    await save_address(case, case.ana, "Ana@Example.org")
    created = await create(case, case.admin_a, {"member": case.ana.id}, {"member": case.bea.id})
    assert states(created) == [(0, "ok"), (1, "without_address")]
    assert (created.status, created.owner_id, created.revision) == ("ok", case.admin_a.id, 1)
    async with case.tx() as tx:
        (resolved,) = await resolve_recipients(tx, draft(case, {"member": case.ana.id}))
    assert (resolved.state, resolved.address) == ("ok", "Ana@example.org")
    # A changed address is used from the next send.
    await save_address(case, case.ana, "ana.new@example.org")
    async with case.tx() as tx:
        (resolved,) = await resolve_recipients(tx, draft(case, {"member": case.ana.id}))
    assert resolved.address == "ana.new@example.org"
    stranger = await principal_with(case, Grant(role="viewer", scope=case.other))
    with pytest.raises(CatalogError) as refused:
        await create(case, case.admin_a, {"member": stranger.id})
    assert (refused.value.status, refused.value.code, refused.value.result) == (
        422,
        "WV-ALERT-RECIPIENT",
        {"recipients": [{"index": 0, "reason": "not_member"}]},
    )


async def test_outside_addresses_need_an_allowed_domain_and_another_administrators_opt_in(mail):
    case = mail
    outside = {"address": "oncall@vendor.example"}
    for setup, reason in ((None, "domain_not_allowed"), ("domains", "no_consent")):
        if setup:
            settings = await allow(case, case.admin_a, ["Vendor.Example."])
            assert (settings.recipient_domains, settings.revision) == (["vendor.example"], 1)
        with pytest.raises(CatalogError) as refused:
            await create(case, case.admin_b, outside)
        assert refused.value.result == {"recipients": [{"index": 0, "reason": reason}]}
    consent = await opt_in(case, case.admin_a, "oncall@vendor.example")
    assert (consent.domain, consent.method, consent.created_by) == (
        "vendor.example",
        "administrator_attested",
        case.admin_a.id,
    )
    with pytest.raises(CatalogError) as own:
        await create(case, case.admin_a, outside)
    assert own.value.result == {"recipients": [{"index": 0, "reason": "consent_by_creator"}]}
    created = await create(case, case.admin_b, outside)
    assert states(created) == [(0, "ok")]
    # Revoking the opt-in stops mail at once, and revoking twice changes nothing.
    revoked = await case.consents.revoke(case.admin_b, case.scope, consent.id, context=CONTEXT)
    assert revoked.revoked_by == case.admin_b.id
    assert await case.consents.revoke(case.admin_a, case.scope, consent.id, context=CONTEXT) == revoked
    read = await case.channels.read(case.admin_a, case.scope, created.id, context=CONTEXT)
    assert states(read) == [(0, "without_consent")]
    # A new opt-in sends again, and a second active one for the same address is refused.
    await opt_in(case, case.admin_a, "oncall@vendor.example")
    assert states(await case.channels.read(case.admin_a, case.scope, created.id, context=CONTEXT)) == [(0, "ok")]
    with pytest.raises(CatalogError) as duplicate:
        await opt_in(case, case.admin_b, "oncall@vendor.example")
    assert (duplicate.value.status, duplicate.value.result["reason"]) == (409, "consent_exists")
    # Removing the domain stops mail at once.
    await allow(case, case.admin_a, [], 1)
    assert states(await case.channels.read(case.admin_a, case.scope, created.id, context=CONTEXT)) == [
        (0, "domain_not_allowed")
    ]


async def test_opt_ins_domains_and_approval_channels_need_environment_manage(mail):
    case = mail
    manager = await principal_with(
        case, Grant(role="alert_manager", scope=case.scope), Grant(role="email_sender", scope=case.scope)
    )
    for call in (
        lambda: allow(case, manager, ["vendor.example"]),
        lambda: opt_in(case, manager, "oncall@vendor.example"),
        lambda: case.consents.revoke(manager, case.scope, uuid4(), context=CONTEXT),
    ):
        with pytest.raises(AccessDenied):
            await call()
    assert (await case.settings.read(manager, case.scope, context=CONTEXT)).revision == 0
    with pytest.raises(CatalogError) as invalid:
        await allow(case, case.admin_a, ["vendor.example", "localhost"])
    assert invalid.value.result == {"field": "recipient_domains", "index": 1}


async def test_changing_approval_channels_tells_the_previous_ones(mail):
    case = mail
    first = await create(case, case.admin_a, {"member": case.ana.id})
    second = await create(case, case.admin_a, {"member": case.ana.id}, name="Second")
    await allow(case, case.admin_a, [], None, [first.id])
    saved = await allow(case, case.admin_a, [], 1, [second.id])
    assert (saved.approval_channel_ids, saved.revision) == ([second.id], 2)
    rows = await owner(
        case,
        "SELECT channel_id,state,recipients_count FROM alert_notifications WHERE tenant_id=:tenant "
        "AND kind='approval_moved'",
    )
    assert rows == [{"channel_id": first.id, "state": "pending", "recipients_count": 1}]
    with pytest.raises(CatalogError) as stale:
        await allow(case, case.admin_a, [], 1, [first.id])
    assert stale.value.code == "WV-REVISION"
    with pytest.raises(CatalogError) as unknown:
        await allow(case, case.admin_a, [], 2, [uuid4()])
    assert unknown.value.code == "WV-ALERT-CHANNEL"
    # An approval channel can't be deleted while approvals use it.
    with pytest.raises(CatalogError) as used:
        await case.channels.delete(case.admin_a, case.scope, second.id, 1, context=CONTEXT)
    assert used.value.result == {"rule_ids": [], "approval": True}


async def test_the_twenty_first_channel_is_refused(mail):
    case = mail
    for index in range(CHANNEL_LIMIT):
        await create(case, case.admin_a, {"member": case.ana.id}, name=f"Channel {index}")
    with pytest.raises(CatalogError) as limit:
        await create(case, case.admin_a, {"member": case.ana.id}, name="One too many")
    assert (limit.value.status, limit.value.code) == (409, "WV-ALERT-LIMIT")
    listed = await case.channels.list(case.admin_a, case.scope, limit=100, cursor=None, context=CONTEXT)
    assert len(listed["items"]) == CHANNEL_LIMIT and listed["next_cursor"] is None


async def test_a_channel_a_rule_uses_cant_be_deleted(mail):
    case = mail
    channel = await create(case, case.admin_a, {"member": case.ana.id})
    rule = AlertRuleDraft.model_validate_json(
        json.dumps(
            {
                "name": "Incidents are open",
                "signal": "incidents.active",
                "comparator": "gt",
                "threshold": 0,
                "severity": "info",
                "channel_ids": [str(channel.id)],
            }
        )
    )
    created = await case.rules.create(case.admin_a, case.scope, rule, "rule", context=CONTEXT)
    with pytest.raises(CatalogError) as used:
        await case.channels.delete(case.admin_a, case.scope, channel.id, 1, context=CONTEXT)
    assert (used.value.status, used.value.code, used.value.result) == (
        409,
        "WV-ALERT-CHANNEL",
        {"rule_ids": [str(created.id)], "approval": False},
    )
    await case.rules.delete(case.admin_a, case.scope, created.id, 1, context=CONTEXT)
    assert await case.channels.delete(case.admin_a, case.scope, channel.id, 1, context=CONTEXT) == Revoked()


async def test_editing_a_channel_makes_the_editor_its_owner(mail):
    case = mail
    channel = await create(case, case.admin_a, {"member": case.ana.id})
    edited = await case.channels.update(
        case.admin_b, case.scope, channel.id, draft(case, {"member": case.ana.id}, name="Renamed"), 1, context=CONTEXT
    )
    assert (edited.owner_id, edited.name, edited.revision) == (case.admin_b.id, "Renamed", 2)
    with pytest.raises(CatalogError) as stale:
        await case.channels.update(
            case.admin_a, case.scope, channel.id, draft(case, {"member": case.ana.id}), 1, context=CONTEXT
        )
    assert stale.value.code == "WV-REVISION"


async def test_a_channel_needs_its_owner_to_be_able_to_send_email_with_its_connection(mail):
    case = mail
    without_mail = await principal_with(
        case, *(Grant(role=role, scope=case.scope) for role in ("alert_manager", "deployer", "viewer"))
    )
    with pytest.raises(AccessDenied):
        await create(case, without_mail, {"member": case.ana.id})
    unusable = await add_connection(case)
    with pytest.raises(CatalogError) as refused:
        await case.channels.create(
            case.admin_a,
            case.scope,
            draft(case, {"member": case.ana.id}).model_copy(update={"connection_revision_id": unusable}),
            "key",
            context=CONTEXT,
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-ALERT-CHANNEL")
    channel = await create(case, case.admin_a, {"member": case.ana.id})
    # The owner loses email.send: the channel says mail can't go out as them anymore.
    (binding,) = await owner(
        case,
        "SELECT id FROM role_bindings WHERE principal_id=:owner AND role='email_sender'",
        owner=case.admin_a.id,
    )
    await case.access.revoke_grant(case.admin, case.scope, binding["id"])
    read = await case.channels.read(case.admin_b, case.scope, channel.id, context=CONTEXT)
    assert read.status == "owner_unauthorized"


async def test_a_test_message_is_queued_once_per_key(mail):
    case = mail
    channel = await create(case, case.admin_a, {"member": case.ana.id}, {"member": case.bea.id})
    queued = await case.channels.test(case.admin_a, case.scope, channel.id, "test-1", context=CONTEXT)
    assert (queued.kind, queued.state, queued.channel_id) == ("test", "pending", channel.id)
    assert await case.channels.test(case.admin_a, case.scope, channel.id, "test-1", context=CONTEXT) == queued
    rows = await owner(
        case, "SELECT kind,state,recipients_count FROM alert_notifications WHERE channel_id=:id", id=channel.id
    )
    assert rows == [{"kind": "test", "state": "pending", "recipients_count": 2}]


async def test_opt_ins_addresses_and_domains_are_audited_with_a_domain_and_a_hash_never_the_address(mail):
    case = mail
    await save_address(case, case.ana, "ana@example.org")
    await allow(case, case.admin_a, ["vendor.example"])
    consent = await opt_in(case, case.admin_a, "oncall@vendor.example")
    await create(case, case.admin_b, {"member": case.ana.id}, {"address": "oncall@vendor.example"})
    await case.consents.revoke(case.admin_b, case.scope, consent.id, context=CONTEXT)
    await save_address(case, case.ana, None)
    rows = await owner(
        case,
        "SELECT action,event->'details' AS details,event::text AS event FROM access_audit "
        "WHERE action LIKE 'alert%' ORDER BY id",
    )
    actions = [row["action"] for row in rows]
    assert actions == [
        "alert_address.save",
        "alert_settings.update",
        "alert_recipient_consent.create",
        "alert_channel.create",
        "alert_recipient_consent.revoke",
        "alert_address.save",
    ]
    assert not any("ana@example.org" in row["event"] or "oncall@vendor.example" in row["event"] for row in rows)
    async with case.tx() as tx:
        expected = await address_hash(tx, "oncall@vendor.example")
    created = rows[2]["details"]
    assert (created["domain"], created["address_hash"], created["method"]) == (
        "vendor.example",
        expected,
        "administrator_attested",
    )
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert rows[3]["details"]["outside"] == [{"domain": "vendor.example", "address_hash": expected}]
    assert rows[3]["details"]["recipients"] == 2 and rows[5]["details"] == {"removed": True}
    # Replays keep IDs only: no address reaches the idempotency records either.
    stored = await owner(case, "SELECT response::text AS response FROM mutation_idempotency WHERE tenant_id=:tenant")
    assert stored and not any("@" in row["response"] for row in stored)


async def test_a_person_reads_and_sets_only_their_own_alert_address(authenticated_client, access_db, provisioned):
    client, tokens, scopes = authenticated_client
    _, owner_sessions, access, _ = access_db
    admin, _ = provisioned
    async with owner_sessions() as session:
        second = await session.scalar(
            text("SELECT principal_id FROM identity_links WHERE subject='1' AND issuer='https://test.invalid'")
        )
    # Both people now have a grant in the first tenant.
    await access.grant(admin, second, Grant(role="viewer", scope=scopes[0]))
    url = f"/api/v1/tenants/{scopes[0].tenant_id}/my-alert-address"
    one, two = ({"Authorization": "Bearer " + token} for token in tokens)
    saved = await client.put(url, json={"address": "first@example.org"}, headers=one)
    assert saved.status_code == 200 and saved.json()["address"] == "first@example.org"
    assert (await client.get(url, headers=two)).json() == {}
    assert (await client.put(url, json={"address": "second@example.org"}, headers=two)).status_code == 200
    assert (await client.get(url, headers=one)).json()["address"] == "first@example.org"
    # Naming another person is refused; their address stays theirs.
    named = await client.put(url, json={"address": "x@example.org", "principal_id": str(second)}, headers=one)
    assert named.status_code == 422
    assert (await client.get(url, headers=two)).json()["address"] == "second@example.org"
    # A tenant the person has no grant in.
    elsewhere = f"/api/v1/tenants/{scopes[1].tenant_id}/my-alert-address"
    assert (await client.get(elsewhere, headers=one)).status_code == 403
