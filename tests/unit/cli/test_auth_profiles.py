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

"""Profile-mode `weave auth` commands and the unchanged alpha6 `--auth-config` contract (simulated provider)."""

import json
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import click
import pytest
from auth_support import (  # noqa: F401
    CLIENT,
    ISSUER,
    SERVER,
    ScriptedConsole,
    auth_env_fixture,
    leaked,
    one_json,
    workspace_tree,
)

from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.profiles import AccountHint, WorkspaceSelection

HINT = AccountHint(subject="subject-1", display_name="ada", issuer=ISSUER, provider_id="acme")
ALPHA6_STATUS_KEYS = {"authenticated", "reauthentication_required", "provider", "target", "account", "expires_at"}


def selection(env, index=0):
    tenant = env.platform.workspaces[0]
    project = tenant["projects"][0]
    environment = project["environments"][index]
    return WorkspaceSelection(
        tenant_id=tenant["id"],
        project_id=project["id"],
        environment_id=environment["id"],
        tenant_name="Acme",
        project_name="Payments",
        environment_name=environment["name"],
    )


# --- alpha6 --auth-config contract -------------------------------------------------------------------------------


@pytest.fixture
def legacy(auth_env):
    """A connection file and file store; every OAuthSession talks to the simulated provider."""
    auth_env.monkeypatch.setattr(OAuthSession, "transport", lambda self: auth_env.platform.transport())
    config = LoginConfig(
        provider_id="acme", issuer=ISSUER, client_id=CLIENT, target=SERVER, account="ops", scopes=("openid",)
    )
    path = auth_env.root / "oauth.json"
    path.write_text(config.model_dump_json())
    common = ["--auth-config", path, "--credential-store", "file", "--credential-file", auth_env.credential_file()]
    return auth_env, common


def test_legacy_login_status_logout_keep_alpha6_json_and_exit_codes(legacy):
    env, common = legacy
    before = env.invoke("auth", "status", *common)
    assert before.exit_code == 1 and not before.stderr
    assert set(one_json(before)) >= ALPHA6_STATUS_KEYS and one_json(before)["authenticated"] is False
    login = env.invoke("auth", "login", *common, "--flow", "device")
    assert login.exit_code == 0, login.stderr
    assert login.stderr.splitlines()[0] == f"Open {ISSUER}/device and enter WXYZ-1234"
    assert len(login.stderr.splitlines()) == 1, "Legacy login prints only the device instruction"
    value = one_json(login)
    assert value["authenticated"] is True and set(value) >= ALPHA6_STATUS_KEYS
    assert value["account"] == "ops" and value["identity"]["subject"] == "subject-1"
    status = env.invoke("auth", "status", *common, "--output", "json")
    assert status.exit_code == 0 and not status.stderr and one_json(status)["authenticated"] is True
    logout = env.invoke("auth", "logout", *common)
    assert logout.exit_code == 0 and not logout.stderr
    assert one_json(logout) == {"logged_out": True, "remote_revocation": "not_requested"}
    assert env.platform.revoked == []
    again = env.invoke("auth", "status", *common)
    assert again.exit_code == 1 and one_json(again)["authenticated"] is False
    assert not leaked(env, login.stdout, login.stderr, status.stdout, logout.stdout)


def test_legacy_revoke_and_failures(legacy):
    env, common = legacy
    assert env.invoke("auth", "login", *common, "--flow", "pkce").exit_code == 0
    assert env.platform.opened == [True]
    revoked = env.invoke("auth", "logout", *common, "--revoke")
    assert one_json(revoked) == {"logged_out": True, "remote_revocation": "confirmed"}
    broken = env.root / "broken.json"
    broken.write_text('{"secret": "do-not-echo"}')
    bad = env.invoke("auth", "status", "--auth-config", broken)
    assert bad.exit_code == 2 and one_json(bad) == {"code": "WV-AUTH-CONFIG", "authenticated": False}
    assert "do-not-echo" not in bad.stdout
    env.platform.device = False
    unavailable = env.invoke("auth", "login", *common, "--flow", "device")
    assert unavailable.exit_code == 2
    assert one_json(unavailable) == {"code": "WV-AUTH-DEVICE-UNAVAILABLE", "authenticated": False}


@pytest.mark.parametrize(
    "extra",
    [["--output", "text"], ["--profile", "prod"], ["--all"], ["--check"]],
)
def test_legacy_rejects_profile_mode_options_as_usage_errors(legacy, extra):
    env, common = legacy
    result = env.invoke("auth", "status", *common, *extra)
    assert result.exit_code == 2
    assert one_json(result)["code"] == "WV-CLI-USAGE"


def test_legacy_ignores_an_inherited_profile_variable(legacy):
    env, common = legacy
    env.monkeypatch.setenv("WEAVE_PROFILE", "prod")
    result = env.invoke("auth", "status", *common)
    assert result.exit_code == 1 and one_json(result)["account"] == "ops"


def test_legacy_switch_account_and_printed_address(legacy):
    env, common = legacy
    result = env.invoke("auth", "login", *common, "--switch-account", "--no-browser")
    assert result.exit_code == 0, result.stderr
    assert parse_qs(urlsplit(env.platform.authorizations[0]).query)["prompt"] == ["login"]
    assert env.platform.opened == [False]
    assert result.stderr.startswith("Open https://login.example/")


def test_credential_store_options_belong_to_connection_files(auth_env):
    auth_env.save_profile()
    result = auth_env.invoke("auth", "status", "--credential-store", "file")
    assert result.exit_code == 2 and not result.stdout
    assert "--credential-store and --credential-file apply with --auth-config only." in result.stderr
    machine = auth_env.invoke("auth", "status", "--credential-store", "file", "--output", "json")
    assert machine.exit_code == 2 and one_json(machine)["code"] == "WV-CLI-USAGE"


# --- login / status / logout -------------------------------------------------------------------------------------


def test_no_profile_points_to_setup(auth_env):
    for command in ("login", "status", "logout", "workspace"):
        result = auth_env.invoke("auth", command, "--output", "json")
        assert result.exit_code == 2, (command, result.stderr)
        value = one_json(result)
        assert value["code"] == "WV-PROFILE-NONE"
        assert "weave auth setup SERVER" in result.stderr
    missing = auth_env.invoke("auth", "status", "--profile", "nope")
    assert missing.exit_code == 2 and "No saved platform has that name." in missing.stderr
    assert "weave auth profiles" in missing.stderr and not missing.stdout


def test_profile_login_records_the_account_and_status_reads_it(auth_env):
    profile = auth_env.save_profile(workspace=selection(auth_env))
    auth_env.monkeypatch.setattr("firefly_weave.cli.auth_flow.gui_browser_available", lambda: False)
    login = auth_env.invoke("auth", "login")
    assert login.exit_code == 0, login.stderr
    assert "WXYZ-1234" in login.stderr and "Signed in to 'prod'" in login.stdout and "as ada" in login.stdout
    assert auth_env.store().get("prod").account == HINT
    status = auth_env.invoke("auth", "status", "--output", "json")
    assert status.exit_code == 0
    value = one_json(status)
    assert value["profile"] == "prod" and value["authenticated"] is True and value["state"] == "signed_in"
    assert value["account"]["display_name"] == "ada" and value["workspace"]["label"] == "Acme / Payments / dev"
    assert value["credentials"] == f"file:{profile.credential_file}"
    text = auth_env.invoke("auth", "status")
    assert "Sign-in:           signed in as ada" in text.stdout and text.exit_code == 0
    assert not leaked(auth_env, login.stdout, login.stderr, status.stdout, text.stdout)


def test_profile_selected_by_environment_variable_case_insensitively(auth_env):
    auth_env.save_profile("Prod", active=False)
    auth_env.save_profile("staging")
    auth_env.monkeypatch.setenv("WEAVE_PROFILE", "prod")
    result = auth_env.invoke("auth", "status", "--output", "json")
    assert result.exit_code == 1 and one_json(result)["profile"] == "Prod"
    assert one_json(result)["state"] == "signed_out"


def test_switch_account_uses_the_browser_with_prompt_login(auth_env):
    auth_env.save_profile()
    auth_env.monkeypatch.setattr("firefly_weave.cli.auth_flow.gui_browser_available", lambda: False)
    result = auth_env.invoke("auth", "login", "--switch-account", "--no-browser", "--output", "json")
    assert result.exit_code == 0, result.stderr
    assert parse_qs(urlsplit(auth_env.platform.authorizations[0]).query)["prompt"] == ["login"]
    assert auth_env.platform.opened == [False]
    refused = auth_env.invoke("auth", "login", "--switch-account", "--flow", "device", "--output", "json")
    assert refused.exit_code == 2 and one_json(refused)["code"] == "WV-AUTH-INPUT"


@pytest.mark.parametrize(
    "flows, requested, allowed_text",
    [
        (["browser"], "device", "browser"),
        (["device"], "browser", "code"),
        (["device"], "pkce", "code"),
    ],
)
@pytest.mark.parametrize("output", ["text", "json"])
def test_login_refuses_a_flow_the_administrator_does_not_allow(auth_env, flows, requested, allowed_text, output):
    profile = auth_env.save_profile(account=HINT, flows=flows)
    auth_env.sign_in_locally(profile)
    result = auth_env.invoke("auth", "login", "--flow", requested, "--output", output)
    assert result.exit_code == 2
    if output == "json":
        value = one_json(result)
        assert value["code"] == "WV-AUTH-FLOW" and value["allowed"] == flows and value["profile"] == "prod"
    else:
        assert result.stdout == "" and "WV-AUTH-FLOW" in result.stderr
    assert allowed_text in result.stderr and "--flow" in result.stderr
    # Refused before contacting the identity provider and without touching the current sign-in.
    assert auth_env.platform.requests == []
    assert auth_env.record(profile).state == "active" and auth_env.store().get("prod").account == HINT


@pytest.mark.parametrize("flows, expected", [(["browser"], "browser"), (["device"], "device")])
def test_login_picks_the_allowed_flow_automatically(auth_env, flows, expected):
    auth_env.save_profile(flows=flows)
    result = auth_env.invoke("auth", "login", "--output", "json")
    assert result.exit_code == 0, result.stderr
    if expected == "browser":
        assert len(auth_env.platform.authorizations) == 1 and "WXYZ-1234" not in result.stderr
    else:
        assert auth_env.platform.authorizations == [] and "WXYZ-1234" in result.stderr


@pytest.mark.parametrize("requested", ["auto", "browser", "device"])
def test_switch_account_on_a_code_only_platform_uses_a_code_and_explains_it(auth_env, requested):
    auth_env.save_profile(flows=["device"])
    result = auth_env.invoke("auth", "login", "--switch-account", "--flow", requested, "--output", "json")
    assert result.exit_code == 0, result.stderr
    assert auth_env.platform.authorizations == [] and "WXYZ-1234" in result.stderr
    assert "choose the account" in result.stderr and one_json(result)["authenticated"] is True
    # The CLI never opens a code's address itself: the guidance must not promise a page that opens.
    guidance = result.stderr.split("To sign in, open this address")[0]
    assert "When you open the sign-in address below" in guidance and "page opens" not in guidance


def test_login_checks_the_provider_before_replacing_the_sign_in(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    auth_env.platform.unreachable_hosts = {"login.example"}
    offline = auth_env.invoke("auth", "login", "--output", "json")
    assert offline.exit_code == 3 and one_json(offline)["code"] == "WV-CONNECT-PROVIDER"
    assert auth_env.record(profile).state == "active"
    auth_env.platform.unreachable_hosts = set()
    auth_env.platform.device = False
    no_device = auth_env.invoke("auth", "login", "--flow", "device", "--output", "json")
    assert no_device.exit_code == 2 and one_json(no_device)["code"] == "WV-AUTH-DEVICE-UNAVAILABLE"
    assert auth_env.record(profile).state == "active" and auth_env.store().get("prod").account == HINT
    auth_env.monkeypatch.setattr("firefly_weave.cli.auth_flow.gui_browser_available", lambda: False)
    fallback = auth_env.invoke("auth", "login", "--output", "json")
    assert fallback.exit_code == 0 and auth_env.platform.authorizations, "No code flow: print the browser address"


def test_cancelled_login_reports_signed_out(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    auth_env.platform.browser_error = KeyboardInterrupt()
    result = auth_env.invoke("auth", "login", "--flow", "browser", "--output", "json")
    assert result.exit_code == 1
    assert one_json(result) == {
        "code": "WV-AUTH-CANCELLED",
        "message": "Cancelled.",
        "profile": "prod",
        "authenticated": False,
    }
    assert "Cancelled." in result.stderr and "You are not signed in to 'prod'" in result.stderr
    assert auth_env.store().get("prod").account is None, "A cancelled attempt replaced the previous sign-in"


def test_status_refreshable_session_is_usable_and_check_verifies_live(auth_env):
    profile = auth_env.save_profile(workspace=selection(auth_env), account=HINT)
    auth_env.sign_in_locally(profile, expires_in=-1)
    status = auth_env.invoke("auth", "status", "--output", "json")
    assert status.exit_code == 0 and one_json(status)["state"] == "expired"
    assert one_json(status)["refresh_available"] is True
    checked = auth_env.invoke("auth", "status", "--check", "--output", "json")
    assert checked.exit_code == 0, checked.stderr
    value = one_json(checked)
    assert value["state"] == "signed_in" and value["identity"]["kind"] == "human"
    assert value["workspace_available"] is True and len(value["workspaces"]) == 2
    assert auth_env.record(profile).access_token.get_secret_value() == auth_env.platform.issued[-1][0]
    text = auth_env.invoke("auth", "status", "--check")
    assert "signed in as ada, verified with the platform (until" in text.stdout
    assert "Workspaces you can use: 2" in text.stdout


def test_status_check_explains_unlinked_and_no_access(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    auth_env.platform.identity_status = 401
    unlinked = auth_env.invoke("auth", "status", "--check", "--output", "json")
    assert unlinked.exit_code == 1
    assert one_json(unlinked)["code"] == "WV-AUTH-NOT-LINKED" and one_json(unlinked)["account"]["subject"]
    auth_env.platform.identity_status = 200
    auth_env.platform.workspaces = []
    empty = auth_env.invoke("auth", "status", "--check", "--output", "json")
    assert empty.exit_code == 0 and one_json(empty)["notice"] == "WV-AUTH-NO-ACCESS"
    assert one_json(empty)["workspaces"] == []


def test_status_signed_out_exits_one_and_all_lists_every_profile(auth_env):
    signed = auth_env.save_profile("prod", credential="prod.json")
    auth_env.save_profile("staging", credential="staging.json", active=False)
    auth_env.sign_in_locally(signed)
    signed_out = auth_env.invoke("auth", "status", "--profile", "staging")
    assert signed_out.exit_code == 1 and "signed out" in signed_out.stdout
    assert "weave auth login --profile staging" in signed_out.stdout
    every = auth_env.invoke("auth", "status", "--all", "--output", "json")
    assert every.exit_code == 0
    assert [(v["profile"], v["active"], v["authenticated"]) for v in one_json(every)] == [
        ("prod", True, True),
        ("staging", False, False),
    ]
    table = auth_env.invoke("auth", "status", "--all")
    assert table.stdout.splitlines()[1].startswith("*  prod")
    conflict = auth_env.invoke("auth", "status", "--all", "--check", "--output", "json")
    assert conflict.exit_code == 2 and one_json(conflict)["flag"] == "--check"


def test_logout_revokes_by_default_and_clears_the_account(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    result = auth_env.invoke("auth", "logout", "--output", "json")
    assert result.exit_code == 0, result.stderr
    assert one_json(result) == {
        "profile": "prod",
        "server": SERVER,
        "logged_out": True,
        "remote_revocation": "confirmed",
        "account": None,
    }
    assert auth_env.platform.revoked == [["refresh_token"]]
    assert auth_env.store().get("prod").account is None and auth_env.record(profile) is None
    auth_env.sign_in_locally(profile)
    kept = auth_env.invoke("auth", "logout", "--no-revoke")
    assert kept.exit_code == 0 and "remote revocation not requested" in kept.stdout
    assert len(auth_env.platform.revoked) == 1
    nothing = auth_env.invoke("auth", "logout")
    assert nothing.exit_code == 0 and "there was no sign-in to revoke" in nothing.stdout


# --- profiles / use / remove -------------------------------------------------------------------------------------


def test_profiles_list_never_reads_credentials(auth_env, monkeypatch):
    auth_env.save_profile("prod", workspace=selection(auth_env), account=HINT)
    auth_env.save_profile("staging", active=False)

    def forbidden(profile):
        raise AssertionError("Listing must not open the credential store")

    monkeypatch.setattr("firefly_weave.cli.auth_flow.open_session", forbidden)
    text = auth_env.invoke("auth", "profiles")
    assert text.exit_code == 0, text.stderr
    lines = text.stdout.splitlines()
    assert lines[0].split() == ["NAME", "SERVER", "WORKSPACE", "ACCOUNT"]
    assert lines[1].startswith("*  prod") and "Acme / Payments / dev" in lines[1] and lines[1].endswith("ada")
    assert lines[2].startswith("   staging")
    value = one_json(auth_env.invoke("auth", "profiles", "--output", "json"))
    assert [(v["profile"], v["active"]) for v in value] == [("prod", True), ("staging", False)]
    assert "authenticated" not in value[0] and value[0]["identity_provider"] == "https://login.example"


def test_profiles_empty_list(auth_env):
    assert one_json(auth_env.invoke("auth", "profiles", "--output", "json")) == []
    assert "weave auth setup SERVER" in auth_env.invoke("auth", "profiles").stdout


def test_use_activates_ignoring_case(auth_env):
    auth_env.save_profile("Prod", active=False)
    result = auth_env.invoke("auth", "use", "PROD", "--output", "json")
    assert result.exit_code == 0 and one_json(result) == {"profile": "Prod", "server": SERVER, "active": True}
    assert auth_env.store().load().active == "Prod"
    missing = auth_env.invoke("auth", "use", "nope", "--output", "json")
    assert missing.exit_code == 2 and one_json(missing)["code"] == "WV-PROFILE-NOT-FOUND"


def test_remove_confirms_signs_out_or_keeps_credentials(auth_env):
    profile = auth_env.save_profile("prod", credential="prod.json")
    auth_env.sign_in_locally(profile)
    unattended = auth_env.invoke("auth", "remove", "prod", "--output", "json")
    assert unattended.exit_code == 2 and one_json(unattended)["flag"] == "--yes"
    declined = auth_env.invoke("auth", "remove", "prod", input="n\n", interactive=True)
    assert declined.exit_code == 1 and auth_env.store().names() == ["prod"]
    removed = auth_env.invoke("auth", "remove", "prod", input="y\n", interactive=True)
    assert removed.exit_code == 0, removed.stderr
    assert "Remove the saved platform 'prod'" in removed.stderr and "You are signed out of it." in removed.stdout
    assert auth_env.store().names() == [] and not profile.credential_file.exists()
    assert auth_env.platform.revoked == [["refresh_token"]]
    staging = auth_env.save_profile("staging", credential="staging.json")
    auth_env.sign_in_locally(staging)
    kept = auth_env.invoke("auth", "remove", "STAGING", "--yes", "--keep-credentials", "--output", "json")
    assert one_json(kept) == {
        "profile": "staging",
        "removed": True,
        "credentials": "kept",
        "remote_revocation": "not_requested",
        "active": None,
    }
    assert auth_env.record(staging).state == "active"


# --- workspace ---------------------------------------------------------------------------------------------------


def test_workspace_by_flags_picker_and_clear(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    chosen = selection(auth_env, 1)
    args = ["--tenant", chosen.tenant_id, "--project", chosen.project_id, "--environment", chosen.environment_id]
    by_flags = auth_env.invoke("auth", "workspace", *args, "--output", "json")
    assert by_flags.exit_code == 0, by_flags.stderr
    assert one_json(by_flags)["workspace"]["label"] == "Acme / Payments / prod"
    picked = auth_env.invoke("auth", "workspace", input="1\n", interactive=True)
    assert picked.exit_code == 0, picked.stderr
    assert "1. Acme / Payments / dev" in picked.stderr and "Currently selected: Acme / Payments / prod" in picked.stderr
    assert "Workspace for 'prod': Acme / Payments / dev." in picked.stdout
    assert auth_env.store().get("prod").workspace.environment_name == "dev"
    cleared = auth_env.invoke("auth", "workspace", "--clear", "--output", "json")
    assert one_json(cleared)["workspace"] is None and auth_env.store().get("prod").workspace is None


def test_workspace_errors(auth_env):
    profile = auth_env.save_profile(account=HINT)
    unattended = auth_env.invoke("auth", "workspace", "--output", "json")
    assert unattended.exit_code == 2 and one_json(unattended)["flag"] == "--tenant/--project/--environment"
    partial = auth_env.invoke("auth", "workspace", "--tenant", UUID(int=1), "--output", "json")
    assert partial.exit_code == 2 and one_json(partial)["flag"] == "--project"
    not_signed_in = auth_env.invoke("auth", "workspace", input="1\n", interactive=True)
    assert not_signed_in.exit_code == 1 and "weave auth login --profile prod" in not_signed_in.stderr
    auth_env.sign_in_locally(profile)
    auth_env.platform.workspaces = []
    empty = auth_env.invoke("auth", "workspace", input="1\n", interactive=True)
    assert empty.exit_code == 1 and "Subject:           subject-1" in empty.stderr
    assert "Support code: WV-AUTH-NO-ACCESS" in empty.stderr


def test_ctrl_c_in_the_workspace_picker(auth_env):
    profile = auth_env.save_profile(account=HINT)
    auth_env.sign_in_locally(profile)
    console = ScriptedConsole("text", [click.Abort()])
    result = auth_env.invoke("auth", "workspace", console=lambda output: console)
    assert result.exit_code == 1 and "Cancelled." in result.stderr and "Traceback" not in result.stderr
    assert auth_env.store().get("prod").workspace is None


def test_auth_group_without_command_shows_help(auth_env):
    result = auth_env.invoke("auth")
    assert result.exit_code == 0
    names = [line.split()[0] for line in result.stdout.split("Commands:")[1].strip().splitlines()]
    assert names == ["setup", "login", "status", "logout", "profiles", "use", "remove", "workspace"]


@pytest.mark.parametrize(
    "args",
    [
        ["auth", "setup", "--unknown=do-not-echo", "--output", "json"],
        ["auth", "use", "--output=json"],
        ["auth", "login", "--auth-config", "missing-do-not-echo.json"],
        ["auth", "status", "--auth-config", "missing-do-not-echo.json", "--profile", "prod"],
        ["auth", "logout", "--auth-config=missing-do-not-echo.json"],
        ["remote", "compile", "--unknown=do-not-echo"],
    ],
)
def test_machine_usage_errors_keep_the_value_free_envelope(auth_env, args):
    result = auth_env.invoke(*args)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "WV-CLI-USAGE" and "do-not-echo" not in result.output
    assert "Usage:" not in result.output


@pytest.mark.parametrize(
    "args, expected",
    [
        (["auth", "use"], "Missing argument 'NAME'"),
        (["auth", "remove"], "Missing argument 'NAME'"),
        (["auth", "setup", "--unknown=do-not-echo"], "No such option '--unknown'"),
        (["auth", "login", "--flow", "sideways"], "Invalid value for '--flow'"),
        (["auth", "workspace", "--tenant", "not-a-uuid"], "Invalid value for '--tenant'"),
        (["auth", "setup", "--auth-config", "missing.json"], "Invalid value for '--auth-config'"),
        (["auth", "sideways"], "No such command 'sideways'"),
    ],
)
def test_human_auth_usage_errors_are_plain_messages_on_stderr(auth_env, args, expected):
    result = auth_env.invoke(*args)
    assert result.exit_code == 2
    assert result.stdout == "" and "WV-CLI-USAGE" not in result.output and "do-not-echo" not in result.output
    assert expected in result.stderr and "Usage:" in result.stderr and "--help" in result.stderr
    assert "Traceback" not in result.output
