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

"""Alert email recipients without a database: allowed domains, who may receive mail when a channel is saved and when
it sends, and the refusals that come before any transaction opens."""

import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import (
    AlertChannelDraft,
    AlertRecipientConsentDraft,
    AlertSettingsUpdate,
)
from firefly_weave.operations.alert_channels import (
    AlertChannelService,
    AlertRecipientConsentService,
    AlertSettingsService,
    Audience,
    ResolvedRecipient,
    normalize_address,
    normalize_domain,
    recipient_allowed,
    refusals,
    resolve,
)

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
ANA, BEA, ADMIN = uuid4(), uuid4(), uuid4()


def channel(*recipients: dict[str, str]) -> AlertChannelDraft:
    return AlertChannelDraft.model_validate_json(
        json.dumps(
            {"kind": "email", "name": "On call", "connection_revision_id": str(uuid4()), "recipients": recipients}
        )
    )


def audience(**changes: Any) -> Audience:
    values: dict[str, Any] = {
        "members": frozenset({ANA, BEA}),
        "addresses": {ANA: "ana@example.org"},
        "domains": ("vendor.example",),
        "consents": {"oncall@vendor.example": ADMIN},
    }
    values.update(changes)
    return Audience(**values)


@pytest.mark.parametrize(
    "address, allowed",
    [
        ("oncall@vendor.example", True),
        ("oncall@eu.vendor.example", True),
        ("oncall@VENDOR.Example", True),
        ("oncall@evilvendor.example", False),
        ("oncall@vendor.example.org", False),
        ("oncall@example", False),
    ],
)
def test_an_address_is_allowed_by_its_exact_domain_or_a_subdomain(address, allowed):
    assert recipient_allowed(address, ["vendor.example"]) is allowed
    assert recipient_allowed(address, []) is False


def test_domains_are_compared_as_lowercase_ascii():
    assert normalize_domain(" Vendor.Example. ") == "vendor.example"
    assert normalize_domain("münchen.de") == "xn--mnchen-3ya.de"
    assert recipient_allowed("team@xn--mnchen-3ya.de", [normalize_domain("München.de")]) is True
    assert normalize_address("Ana.Lopez@Vendor.EXAMPLE") == "Ana.Lopez@vendor.example"


@pytest.mark.parametrize("value", ["localhost", "a..example", "-vendor.example", "vendor.example/x", "x" * 64 + ".com"])
def test_a_domain_that_isnt_a_domain_is_refused(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_a_member_recipient_must_have_a_grant_in_the_project():
    stranger = uuid4()
    draft = channel({"member": str(ANA)}, {"member": str(stranger)})
    assert refusals(draft, audience(), actor=ANA, sole_administrator=False) == [{"index": 1, "reason": "not_member"}]


def test_a_channel_of_project_members_needs_no_allowed_domain():
    draft = channel({"member": str(ANA)}, {"member": str(BEA)})
    assert refusals(draft, audience(domains=(), consents={}), actor=ANA, sole_administrator=False) == []


def test_an_outside_address_outside_the_allowed_domains_is_refused():
    draft = channel({"member": str(ANA)}, {"address": "someone@example.net"})
    assert refusals(draft, audience(), actor=ANA, sole_administrator=False) == [
        {"index": 1, "reason": "domain_not_allowed"}
    ]


def test_an_outside_address_without_an_opt_in_is_refused():
    draft = channel({"address": "other@vendor.example"}, {"address": "OnCall@Vendor.Example"})
    # The opt-in of a different local part doesn't count; the domain is compared without case.
    assert refusals(
        draft, audience(consents={"OnCall@vendor.example": ADMIN}), actor=ANA, sole_administrator=False
    ) == [{"index": 0, "reason": "no_consent"}]


def test_the_person_who_recorded_the_opt_in_cant_add_that_address_when_another_administrator_exists():
    draft = channel({"address": "oncall@vendor.example"})
    assert refusals(draft, audience(), actor=ADMIN, sole_administrator=False) == [
        {"index": 0, "reason": "consent_by_creator"}
    ]
    # The only administrator may, and an address already on the channel stays.
    assert refusals(draft, audience(), actor=ADMIN, sole_administrator=True) == []
    kept = frozenset({"oncall@vendor.example"})
    assert refusals(draft, audience(), actor=ADMIN, sole_administrator=False, kept=kept) == []
    assert refusals(draft, audience(), actor=ANA, sole_administrator=False) == []


def test_a_member_recipients_address_is_the_one_they_saved_never_one_the_creator_typed():
    stranger = uuid4()
    draft = channel(
        {"member": str(ANA)},
        {"member": str(BEA)},
        {"member": str(stranger)},
        {"address": "oncall@vendor.example"},
        {"address": "late@vendor.example"},
        {"address": "x@example.net"},
    )
    assert resolve(draft, audience()) == [
        ResolvedRecipient(0, "ana@example.org", "ok"),
        ResolvedRecipient(1, None, "without_address"),
        ResolvedRecipient(2, None, "not_member"),
        ResolvedRecipient(3, "oncall@vendor.example", "ok"),
        ResolvedRecipient(4, None, "without_consent"),
        ResolvedRecipient(5, None, "domain_not_allowed"),
    ]


class Unopened:
    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        raise AssertionError("a refused call opened a transaction")
        yield


def person(*roles: str) -> Principal:
    return Principal(id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=SCOPE) for role in roles))


CONTEXT = AuditContext()
AUTHORIZATION = AuthorizationService()
CHANNELS = AlertChannelService(Unopened(), AUTHORIZATION, None)  # type: ignore[arg-type]
SETTINGS = AlertSettingsService(Unopened(), AUTHORIZATION)  # type: ignore[arg-type]
CONSENTS = AlertRecipientConsentService(Unopened(), AUTHORIZATION)  # type: ignore[arg-type]
DRAFT = channel({"member": str(ANA)})
CONSENT = AlertRecipientConsentDraft(address="oncall@vendor.example", statement="Agreed on the support call")


def call(name: str, actor: Principal) -> Any:
    identifier: UUID = uuid4()
    return {
        "channels.list": lambda: CHANNELS.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT),
        "channels.read": lambda: CHANNELS.read(actor, SCOPE, identifier, context=CONTEXT),
        "channels.create": lambda: CHANNELS.create(actor, SCOPE, DRAFT, "key", context=CONTEXT),
        "channels.update": lambda: CHANNELS.update(actor, SCOPE, identifier, DRAFT, 1, context=CONTEXT),
        "channels.delete": lambda: CHANNELS.delete(actor, SCOPE, identifier, 1, context=CONTEXT),
        "channels.test": lambda: CHANNELS.test(actor, SCOPE, identifier, "key", context=CONTEXT),
        "settings.read": lambda: SETTINGS.read(actor, SCOPE, context=CONTEXT),
        "settings.update": lambda: SETTINGS.update(
            actor, SCOPE, AlertSettingsUpdate(recipient_domains=[], approval_channel_ids=[]), None, context=CONTEXT
        ),
        "consents.list": lambda: CONSENTS.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT),
        "consents.create": lambda: CONSENTS.create(actor, SCOPE, CONSENT, "key", context=CONTEXT),
        "consents.revoke": lambda: CONSENTS.revoke(actor, SCOPE, identifier, context=CONTEXT),
    }[name]()


@pytest.mark.parametrize(
    "name",
    [
        "channels.list",
        "channels.read",
        "channels.create",
        "channels.update",
        "channels.delete",
        "channels.test",
        "settings.read",
        "settings.update",
        "consents.list",
        "consents.create",
        "consents.revoke",
    ],
)
async def test_a_person_without_alert_capabilities_is_refused_before_anything_opens(name):
    with pytest.raises(AccessDenied):
        await call(name, person("viewer", "operator", "email_sender", "tenant_admin"))


@pytest.mark.parametrize("name", ["channels.create", "channels.update"])
async def test_a_channel_needs_its_owner_to_be_able_to_send_email(name):
    with pytest.raises(AccessDenied):
        await call(name, person("alert_manager"))


@pytest.mark.parametrize("name", ["settings.update", "consents.create", "consents.revoke"])
async def test_allowed_domains_approval_channels_and_opt_ins_need_environment_manage(name):
    with pytest.raises(AccessDenied):
        await call(name, person("alert_manager", "email_sender"))
