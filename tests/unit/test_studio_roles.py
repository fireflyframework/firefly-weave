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

"""Approval and email authority are explicit and independent of operator grants."""

from uuid import uuid4

import pytest

from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope


def test_business_roles_are_explicit_and_separate():
    assert "human_task.complete" in ROLE_CAPABILITIES["task_participant"]
    assert "human_task.manage" in ROLE_CAPABILITIES["task_manager"]
    assert "human_task.complete" not in ROLE_CAPABILITIES["task_manager"]
    assert "email.send" in ROLE_CAPABILITIES["email_sender"]
    assert "email.send" not in ROLE_CAPABILITIES["email_reader"]
    for role in ("viewer", "operator", "worker", "developer", "tenant_admin", "platform_admin"):
        assert "human_task.complete" not in ROLE_CAPABILITIES[role]
        assert "email.send" not in ROLE_CAPABILITIES[role]


def test_worker_cannot_gain_human_permission_through_role():
    scope = Scope(tenant_id=uuid4())
    actor = Principal(id=uuid4(), kind="worker", grants=(Grant(role="task_participant", scope=scope),))
    with pytest.raises(AccessDenied):
        AuthorizationService().require(actor, scope, "human_task.complete")


def test_standalone_schema_export_includes_new_public_contracts():
    from firefly_weave.contracts.schema_export import export_schemas

    schemas = export_schemas()
    assert {
        "human-task",
        "complete-human-task",
        "assignment-binding",
        "manual-control-request",
        "email-send-request",
        "email-conversation-detail",
        "email-source-request",
        "identity-view",
    } <= schemas.keys()
    assert "expected_revision" in schemas["complete-human-task"]["required"]


def test_mail_egress_policy_comes_from_server_environment(monkeypatch):
    from firefly_weave.settings import Settings

    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test")
    monkeypatch.setenv("WEAVE_MAIL_PRIVATE_NETWORKS", '["10.0.0.0/24"]')
    monkeypatch.setenv("WEAVE_MAIL_ALLOWED_PORTS", "[465, 993]")
    monkeypatch.setenv("WEAVE_MAIL_ALLOW_LOCAL_FIXTURE", "false")
    settings = Settings.from_env()
    assert settings.mail_private_networks == ("10.0.0.0/24",)
    assert settings.mail_allowed_ports == (465, 993)
    assert settings.mail_allow_local_fixture is False
