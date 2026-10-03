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

"""Shared sign-in orchestration: sessions, scope-free identity, workspaces and linking errors (simulated API)."""

import time
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.identity import IdentityView
from firefly_weave.contracts.public import Problem
from firefly_weave.sdk import sign_in
from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.credentials import CredentialRecord, FileCredentialStore
from firefly_weave.sdk.errors import WeaveError
from firefly_weave.sdk.profiles import AccountHint, PlatformProfile, WorkspaceSelection
from firefly_weave.sdk.sign_in import (
    IdentityCheck,
    SignInError,
    WorkspaceOption,
    check_identity,
    read_identity,
    session_for,
    verify_sign_in,
    workspace_options,
)

ISSUER = "https://login.example/realms/acme"
HINT = AccountHint(subject="sub-1", display_name="ada", issuer=ISSUER, provider_id="acme")


def platform(tmp_path=None, **changes):
    login = LoginConfig(
        provider_id="acme",
        issuer=ISSUER,
        client_id="weave-cli",
        target="https://weave.example",
        account="prod",
        scopes=("openid",),
    )
    if tmp_path is not None:
        changes = {"credential_store": "file", "credential_file": tmp_path / "tokens.json", **changes}
    return PlatformProfile(name="prod", login=login, source="server", **changes)


def ids():
    return [uuid4() for _ in range(6)]


def identity_document(workspaces=None):
    return {
        "principal_id": str(uuid4()),
        "kind": "human",
        "grants": [],
        "workspaces": workspaces if workspaces is not None else [],
        "truncated": False,
    }


def nested():
    t1, t2, p1, p2, e1, e2 = ids()
    e3 = uuid4()
    return [
        {
            "id": str(t2),
            "name": "Zeta Corp",
            "projects": [{"id": str(p2), "name": "Ops", "environments": [{"id": str(e3), "name": "prod"}]}],
        },
        {
            "id": str(t1),
            "name": "Acme",
            "projects": [
                {
                    "id": str(p1),
                    "name": "Payments",
                    "environments": [{"id": str(e2), "name": "staging"}, {"id": str(e1), "name": "dev"}],
                },
                {"id": str(uuid4()), "name": "Empty", "environments": []},
            ],
        },
    ]


class Tokens:
    def __init__(self, authenticated=True):
        self.targets, self.authenticated = [], authenticated

    async def get_access_token(self, target):
        self.targets.append(target)
        return "access-sentinel"

    def status(self):
        return {"authenticated": self.authenticated}


def api(status=200, body=None, calls=None):
    def receive(request):
        if calls is not None:
            calls.append(request)
        if status != 200:
            return httpx.Response(
                status,
                json={"status": status, "code": "WV-UNAUTHENTICATED", "message": "Authentication failed"},
                headers={"X-Weave-Wire-Version": "weave/api-v1"},
            )
        return httpx.Response(200, json=body if body is not None else identity_document(nested()))

    return httpx.MockTransport(receive)


# --- sessions ----------------------------------------------------------------------------------------------------


def test_session_uses_the_profile_login_and_its_explicit_store(tmp_path, monkeypatch):
    factory = lambda: httpx.MockTransport(lambda request: httpx.Response(500))  # noqa: E731
    session = session_for(platform(tmp_path), transport_factory=factory)
    assert isinstance(session, OAuthSession) and session.config == platform(tmp_path).login
    assert isinstance(session.store, FileCredentialStore) and session.store.path == tmp_path / "tokens.json"
    assert session.transport_factory is factory
    created = []

    class Native:
        def __init__(self):
            created.append(self)

    monkeypatch.setattr(sign_in, "NativeCredentialStore", Native)
    native = session_for(platform())
    assert native.store is created[0] and native.transport_factory is None


# --- identity ----------------------------------------------------------------------------------------------------


async def test_identity_needs_no_workspace_scope():
    calls, tokens = [], Tokens()
    identity = await read_identity(platform(), tokens, transport=api(calls=calls))
    assert isinstance(identity, IdentityView) and len(identity.workspaces) == 2
    (request,) = calls
    assert str(request.url) == "https://weave.example/api/v1/identity"
    assert request.headers["authorization"] == "Bearer access-sentinel"
    assert tokens.targets == ["https://weave.example"]


async def test_scope_free_client_still_requires_scope_for_scoped_operations():
    async with WeaveClient("https://weave.example", lambda: "token", None, transport=api()) as client:
        with pytest.raises(ValueError):
            await client.invoke("catalog.read")
        assert isinstance(await client.invoke("identity.read"), IdentityView)
    tenant_only = Scope(tenant_id=uuid4())
    async with WeaveClient("https://weave.example", lambda: "token", tenant_only, transport=api()) as client:
        with pytest.raises(ValueError):
            await client.invoke("runs.read", identifier=uuid4())


async def test_unlinked_identity_after_valid_local_credential_is_named(tmp_path):
    tmp_path.chmod(0o700)
    profile = platform(tmp_path, account=HINT)
    session = session_for(profile)
    session.store.save(
        profile.login.binding,
        CredentialRecord(
            binding=profile.login.binding,
            state="active",
            access_token="access-sentinel",
            refresh_token="refresh-sentinel",
            expires_at=time.time() + 300,
            scopes=("openid",),
        ),
    )
    with pytest.raises(SignInError) as failure:
        await read_identity(profile, session, transport=api(401))
    assert failure.value.code == "WV-AUTH-NOT-LINKED" and failure.value.exit_code == 1
    assert failure.value.account == HINT
    assert "sentinel" not in str(failure.value)


@pytest.mark.parametrize(
    "provider, status",
    [(Tokens(authenticated=False), 401), (lambda: "static-token", 401), (Tokens(), 403), (Tokens(), 500)],
)
async def test_other_identity_failures_are_not_relabeled(provider, status):
    with pytest.raises(WeaveError) as failure:
        await read_identity(platform(account=HINT), provider, transport=api(status))
    assert failure.value.status == status and not isinstance(failure.value, SignInError)


def test_failing_local_status_is_not_treated_as_valid():
    class Broken(Tokens):
        def status(self):
            raise OSError("store unavailable")

    error = WeaveError(Problem(status=401, code="WV-UNAUTHENTICATED", message="Authentication failed"))
    assert sign_in.identity_failure(error, Broken(), HINT) is None
    assert sign_in.identity_failure(error, Tokens(), None).code == "WV-AUTH-NOT-LINKED"
    assert sign_in.identity_failure(ValueError("x"), Tokens(), HINT) is None


# --- workspaces --------------------------------------------------------------------------------------------------


def test_workspaces_flatten_to_sorted_labeled_options():
    identity = IdentityView.model_validate_json(httpx.Response(200, json=identity_document(nested())).content)
    options = workspace_options(identity)
    assert [option.label for option in options] == [
        "Acme / Payments / dev",
        "Acme / Payments / staging",
        "Zeta Corp / Ops / prod",
    ]
    first = options[0]
    assert isinstance(first, WorkspaceOption)
    assert (first.tenant_name, first.project_name, first.environment_name) == ("Acme", "Payments", "dev")
    tenant = identity.workspaces[1]
    assert first.tenant_id == tenant.id and first.project_id == tenant.projects[0].id
    assert first.environment_id == tenant.projects[0].environments[1].id
    selection = first.selection()
    assert isinstance(selection, WorkspaceSelection)
    assert (selection.environment_id, selection.environment_name) == (first.environment_id, "dev")
    duplicated = identity.model_copy(update={"workspaces": identity.workspaces + identity.workspaces})
    assert workspace_options(duplicated) == options


def test_unbounded_server_names_never_break_the_saved_selection():
    t, p, e = ids()[:3]
    identity = IdentityView.model_validate_json(
        httpx.Response(
            200,
            json=identity_document(
                [
                    {
                        "id": str(t),
                        "name": "T" * 300,
                        "projects": [
                            {"id": str(p), "name": "bad\nname", "environments": [{"id": str(e), "name": "e"}]}
                        ],
                    }
                ]
            ),
        ).content
    )
    (option,) = workspace_options(identity)
    selection = option.selection()
    assert selection.tenant_name is None and selection.project_name is None and selection.environment_name == "e"


async def test_no_workspaces_is_a_result_flag_that_keeps_the_identity():
    empty = await verify_sign_in(platform(), Tokens(), transport=api(body=identity_document([])))
    assert isinstance(empty, IdentityCheck)
    assert empty.code == "WV-AUTH-NO-ACCESS" and empty.workspaces == [] and empty.identity.kind == "human"
    found = await verify_sign_in(platform(), Tokens(), transport=api())
    assert found.code is None and len(found.workspaces) == 3 and not found.truncated
    assert check_identity(found.identity) == found


def test_sign_in_error_shape():
    error = SignInError("WV-AUTH-NOT-LINKED", HINT)
    assert str(error) == "WV-AUTH-NOT-LINKED" and error.account == HINT and error.exit_code == 1
    assert SignInError("WV-AUTH-NOT-LINKED").account is None
