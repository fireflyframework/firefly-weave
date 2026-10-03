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

"""`weave auth setup`: the four-step wizard, unattended flags, JSON purity and Ctrl+C (simulated platform)."""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import click
import pytest
from auth_support import (  # noqa: F401
    ISSUER,
    SERVER,
    ScriptedConsole,
    auth_env_fixture,
    leaked,
    one_json,
    option,
    workspace_tree,
)

from firefly_weave.cli.auth_setup import suggest_name

SRC = Path(__file__).resolve().parents[3] / "src"
SUMMARY_KEYS = {"profile", "server", "active", "authenticated", "account", "workspace", "saved", "next"}


def setup_args(env, *extra, server="weave.example"):
    args = ["auth", "setup"]
    if server is not None:
        args.append(server)
    return [*args, "--credential-store", "file", "--credential-file", env.credential_file(), *extra]


def ids(env, index):
    tenant = env.platform.workspaces[0]
    project = tenant["projects"][0]
    environment = project["environments"][index]
    return ["--tenant", tenant["id"], "--project", project["id"], "--environment", environment["id"]]


# --- the interactive wizard --------------------------------------------------------------------------------------


def test_interactive_wizard_asks_each_step_and_saves_no_secrets(auth_env):
    auth_env.platform.sign_in = [option(), option(provider_id="partner", display_name="Partner Login")]
    answers = "\n".join(["weave.example", "1", "", "y", "2"]) + "\n"
    result = auth_env.invoke(*setup_args(auth_env, server=None), input=answers, interactive=True)
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    for number, title in enumerate(("Server", "Review", "Sign in", "Workspace"), 1):
        assert f"Step {number} of 4 · {title}" in result.stderr
    for prompt in ("Server address", "Sign-in option number", "Name for this platform", "Trust", "Workspace number"):
        assert prompt in result.stderr and prompt not in result.stdout
    assert "Identity provider: https://login.example" in result.stderr
    assert "no passwords or tokens" in result.stderr
    assert "Platform 'acme-weave' is set up and active." in result.stdout
    assert "Acme / Payments / prod" in result.stdout and "Next:" in result.stdout
    assert auth_env.platform.opened == [True]
    profile = auth_env.store().get("acme-weave")
    assert profile.source == "server" and profile.login.account == "acme-weave"
    assert profile.workspace.environment_name == "prod" and profile.account.subject == "subject-1"
    assert auth_env.store().load().active == "acme-weave"
    raw = auth_env.profiles_file.read_text()
    assert not leaked(auth_env, raw, result.stdout, result.stderr)
    assert auth_env.record(profile).state == "active"


def test_interactive_server_errors_are_explained_and_asked_again(auth_env):
    auth_env.platform.unreachable_hosts = {"down.example"}
    console = ScriptedConsole("text", ["http://example.com", "down.example", "weave.example", "", True, "1"])
    result = auth_env.invoke(*setup_args(auth_env, server=None), console=lambda output: console)
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert "Plain http:// is only allowed for this computer" in result.stderr
    assert "The server could not be reached" in result.stderr
    assert console.asked.count("Server address (for example weave.example.com)") == 3


def test_interactive_name_conflict_asks_before_replacing(auth_env):
    auth_env.save_profile("acme-weave", active=False)
    console = ScriptedConsole("text", ["acme-weave", False, "acme-eu", True, "1"])
    result = auth_env.invoke(*setup_args(auth_env), console=lambda output: console)
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert auth_env.store().names() == ["acme-eu", "acme-weave"]
    assert any("already named 'acme-weave'" in question for question in console.asked)


def test_platform_names_cannot_send_terminal_control_sequences(auth_env):
    # A tenant name carrying an OSC 52 clipboard write and a project name with a C1 control (CSI).
    auth_env.platform.workspaces = workspace_tree(
        ("Acme\x1b]52;c;ZXZpbA==\x07", "Pay\x9bments", "dev"), ("Acme", "Payments", "prod")
    )
    console = ScriptedConsole("text", ["", True, "1"])
    result = auth_env.invoke(*setup_args(auth_env), console=lambda output: console)
    assert result.exit_code == 0, (result.stderr, result.exception)
    printed = result.stdout + result.stderr
    assert "\x1b" not in printed and "\x07" not in printed and "\x9b" not in printed
    assert "Acme?]52;c;ZXZpbA==? / Pay?ments / dev" in result.stderr


def test_declining_trust_saves_nothing(auth_env):
    console = ScriptedConsole("text", ["", False])
    result = auth_env.invoke(*setup_args(auth_env), console=lambda output: console)
    assert result.exit_code == 1
    assert "The identity provider was not confirmed." in result.stderr and "Nothing was saved." in result.stderr
    assert not auth_env.profiles_file.exists()


# --- unattended and JSON ------------------------------------------------------------------------------------------


def test_unattended_device_setup_prints_one_json_document(auth_env):
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", "device", "--output", "json", *ids(auth_env, 0)))
    assert result.exit_code == 0, (result.stdout, result.stderr)
    value = one_json(result)
    assert set(value) == SUMMARY_KEYS
    assert value["profile"] == "acme-weave" and value["server"] == SERVER and value["active"] is True
    assert value["authenticated"] is True and value["account"]["subject"] == "subject-1"
    assert value["workspace"]["label"] == "Acme / Payments / dev"
    assert value["saved"] == {
        "profiles_file": str(auth_env.profiles_file),
        "credentials": f"file:{auth_env.credential_file()}",
    }
    assert value["next"] == ["weave studio", "weave auth status --profile acme-weave --check"]
    assert "WXYZ-1234" in result.stderr and ISSUER + "/device" in result.stderr
    assert "Step 3 of 4 · Sign in" in result.stderr and "WXYZ-1234" not in result.stdout
    assert not leaked(auth_env, result.stdout, result.stderr)


def test_text_summary_goes_to_stdout_and_guidance_to_stderr(auth_env):
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login"))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("Platform 'acme-weave' is set up and active.")
    assert "Step" not in result.stdout and "Signed in:   no" in result.stdout
    assert "weave auth login --profile acme-weave" in result.stdout
    assert "Skipped (--no-login)." in result.stderr and "Skipped: sign in first." in result.stderr
    assert auth_env.platform.authorizations == []


@pytest.mark.parametrize(
    "requested, gui, no_browser, expected",
    [
        ("auto", True, False, "browser"),
        ("auto", False, False, "device"),
        ("auto", True, True, "device"),
        ("browser", False, True, "browser"),
        ("device", True, False, "device"),
    ],
)
def test_flow_choice(auth_env, requested, gui, no_browser, expected):
    auth_env.monkeypatch.setattr("firefly_weave.cli.auth_flow.gui_browser_available", lambda: gui)
    extra = ["--yes", "--skip-workspace", "--flow", requested, "--output", "json"]
    result = auth_env.invoke(*setup_args(auth_env, *extra, *(["--no-browser"] if no_browser else [])))
    assert result.exit_code == 0, result.stderr
    if expected == "browser":
        assert auth_env.platform.opened == [not no_browser]
        assert "https://login.example/realms/acme/protocol/openid-connect/auth?" in result.stderr
    else:
        assert auth_env.platform.authorizations == [] and "WXYZ-1234" in result.stderr


def test_auto_without_browser_and_without_device_prints_the_address(auth_env):
    auth_env.platform.sign_in = [option(flows=["browser"])]
    auth_env.monkeypatch.setattr("firefly_weave.cli.auth_flow.gui_browser_available", lambda: False)
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--skip-workspace", "--output", "json"))
    assert result.exit_code == 0, result.stderr
    assert "Sign-in methods:   browser on this computer" in result.stderr
    assert auth_env.platform.opened == [True]


@pytest.mark.parametrize("flows", [["browser"], ["device"], ["browser", "device"]])
def test_setup_saves_the_sign_in_flows_the_server_allows(auth_env, flows):
    auth_env.platform.sign_in = [option(flows=flows)]
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--name", "acme", "--output", "json"))
    assert result.exit_code == 0, result.stderr
    assert auth_env.store().get("acme").flows == flows
    assert json.loads(auth_env.profiles_file.read_text())["profiles"]["acme"]["flows"] == flows


@pytest.mark.parametrize(
    "flows, requested, allowed_text", [(["browser"], "device", "browser"), (["device"], "browser", "code")]
)
def test_explicit_flow_the_administrator_does_not_allow_is_refused_before_saving(
    auth_env, flows, requested, allowed_text
):
    auth_env.platform.sign_in = [option(flows=flows)]
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", requested, "--output", "json"))
    assert result.exit_code == 2
    value = one_json(result)
    # The administrator refused it (not the identity provider), and the identity provider is never asked.
    assert value["code"] == "WV-AUTH-FLOW" and value["allowed"] == flows
    assert "administrator" in value["message"] and allowed_text in value["message"]
    assert value["saved"] is False and not auth_env.profiles_file.exists()
    assert not any("login.example" in url for _, url in auth_env.platform.requests)


def test_explicit_device_flow_the_identity_provider_does_not_offer_is_refused_before_saving(auth_env):
    auth_env.platform.device = False
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", "device", "--output", "json"))
    assert result.exit_code == 2
    assert one_json(result)["code"] == "WV-AUTH-DEVICE-UNAVAILABLE"
    assert one_json(result)["saved"] is False and not auth_env.profiles_file.exists()


@pytest.mark.parametrize(
    "extra, flag",
    [
        ([], "SERVER"),
        (["weave.example"], "--yes"),
        (["weave.example", "--yes", "--tenant", "00000000-0000-0000-0000-000000000001"], "--project"),
        (["weave.example", "--credential-store", "native", "--credential-file", "/tmp/x"], "--credential-store file"),
    ],
)
@pytest.mark.parametrize("output", ["text", "json"])
def test_missing_input_never_prompts_and_names_the_flag(auth_env, extra, flag, output):
    if "--credential-store" in extra:
        args = ["auth", "setup", *extra, "--output", output]
    else:
        args = setup_args(auth_env, *extra[1:], "--output", output, server=extra[0] if extra else None)
    # JSON never prompts, even in a terminal; text never prompts without one.
    result = auth_env.invoke(*args, interactive=output == "json")
    assert result.exit_code == 2, (result.stdout, result.stderr)
    assert "Server address" not in result.stderr and "[y/N]" not in result.stderr
    if output == "json":
        value = one_json(result)
        assert value["code"] == "WV-AUTH-INPUT" and value["flag"] == flag
    else:
        assert flag in result.stderr and "Support code: WV-AUTH-INPUT" in result.stderr and not result.stdout


def test_several_providers_need_provider_flag_unattended(auth_env):
    auth_env.platform.sign_in = [option(), option(provider_id="partner", display_name="Partner")]
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 2 and one_json(result)["flag"] == "--provider"
    chosen = auth_env.invoke(
        *setup_args(auth_env, "--yes", "--provider", "partner", "--skip-workspace", "--output", "json")
    )
    assert chosen.exit_code == 0, chosen.stderr
    assert auth_env.store().get("acme-weave").login.provider_id == "partner"
    unknown = auth_env.invoke(*setup_args(auth_env, "--yes", "--provider", "nope", "--output", "json"))
    assert unknown.exit_code == 2 and "partner" in one_json(unknown)["message"]


def test_several_workspaces_without_flags_report_the_saved_state(auth_env):
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 2
    value = one_json(result)
    assert value["code"] == "WV-AUTH-INPUT" and value["flag"] == "--tenant/--project/--environment"
    assert value["saved"] is True and value["authenticated"] is True and value["profile"] == "acme-weave"
    assert "weave auth workspace --profile acme-weave" in result.stderr


def test_unauthorized_workspace_flags_are_refused(auth_env):
    args = ["--tenant", str(UUID(int=1)), "--project", str(UUID(int=2)), "--environment", str(UUID(int=3))]
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json", *args))
    assert result.exit_code == 1 and one_json(result)["code"] == "WV-AUTH-WORKSPACE"
    assert auth_env.store().get("acme-weave").workspace is None


def test_single_workspace_is_selected_without_asking(auth_env):
    auth_env.platform.workspaces = workspace_tree(("Acme", "Payments", "dev"))
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 0, result.stderr
    assert one_json(result)["workspace"]["label"] == "Acme / Payments / dev"
    assert "Using the only workspace available" in result.stderr


def test_no_workspace_access_explains_and_succeeds(auth_env):
    auth_env.platform.workspaces = []
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 0, result.stderr
    value = one_json(result)
    assert value["workspace"] is None and value["notice"] == "WV-AUTH-NO-ACCESS" and value["authenticated"]
    assert value["next"] == ["weave auth workspace --profile acme-weave   (after an administrator grants access)"]
    assert "no access to any workspace" in result.stderr
    assert f"Identity provider: {ISSUER}" in result.stderr and "Subject:           subject-1" in result.stderr


def test_unlinked_account_is_explained_with_the_hint(auth_env):
    auth_env.platform.identity_status = 401
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 1
    value = one_json(result)
    assert value["code"] == "WV-AUTH-NOT-LINKED" and value["account"]["subject"] == "subject-1"
    assert value["saved"] is True and value["authenticated"] is True and value["profile"] == "acme-weave"
    assert "does not recognize your account" in result.stderr and "Subject:           subject-1" in result.stderr


@pytest.mark.parametrize(
    "problem, code, exit_code",
    [
        ("unreachable", "WV-CONNECT-UNREACHABLE", 3),
        ("no-sign-in", "WV-CONNECT-NO-SIGN-IN", 3),
        ("insecure", "WV-CONNECT-INSECURE", 2),
        ("http-issuer", "WV-CONNECT-PROVIDER", 3),
    ],
)
def test_server_problems_are_stable_codes_with_plain_messages(auth_env, problem, code, exit_code):
    server = "weave.example"
    if problem == "unreachable":
        auth_env.platform.unreachable_hosts = {"weave.example"}
    elif problem == "no-sign-in":
        auth_env.platform.sign_in = []
    elif problem == "insecure":
        server = "http://weave.example"
    else:
        auth_env.platform.sign_in = [option(issuer="http://localhost:8080/realms/acme", allow_loopback_http=True)]
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json", server=server))
    assert result.exit_code == exit_code, result.stderr
    value = one_json(result)
    assert value["code"] == code and value["saved"] is False and "Traceback" not in result.stderr
    assert "Nothing was saved." in result.stderr
    assert not auth_env.profiles_file.exists()


def test_existing_name_needs_replace(auth_env):
    first = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--name", "Prod"))
    assert first.exit_code == 0, first.stderr
    again = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--name", "prod", "--output", "json"))
    assert again.exit_code == 2 and one_json(again)["code"] == "WV-PROFILE-EXISTS"
    replaced = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--name", "prod", "--replace"))
    assert replaced.exit_code == 0, replaced.stderr
    assert auth_env.store().names() == ["Prod"]
    suggested = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--output", "json"))
    assert one_json(suggested)["profile"] == "acme-weave"
    second = auth_env.invoke(*setup_args(auth_env, "--yes", "--no-login", "--output", "json"))
    assert one_json(second)["profile"] == "acme-weave-2", "An unattended setup never replaces silently"


def test_name_suggestions_are_valid_profile_names():
    assert suggest_name("Acme Weave (EU)", SERVER, set()) == "acme-weave-eu"
    assert suggest_name(None, "https://weave.acme.example:8443", set()) == "weave.acme.example"
    assert suggest_name("!!!", "https://[::1]", set()) == "1"
    assert suggest_name("Acme", SERVER, {"acme", "acme-2"}) == "acme-3"
    assert suggest_name("Acme", SERVER, {"acme"}, reuse=True) == "acme"


def test_private_credential_folder_is_required(auth_env, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    result = auth_env.invoke(
        "auth", "setup", "weave.example", "--yes", "--credential-store", "file",
        "--credential-file", shared / "tokens.json", "--output", "json",
    )  # fmt: skip
    assert result.exit_code == 2 and one_json(result)["code"] == "WV-AUTH-STORE"


# --- connection file import --------------------------------------------------------------------------------------


def connection_file(env, **changes):
    from firefly_weave.sdk.auth import LoginConfig

    login = LoginConfig(
        provider_id="acme",
        issuer=ISSUER,
        client_id="weave-cli",
        target=SERVER,
        account="someone-else",
        scopes=("openid",),
        **changes,
    )
    path = env.root / "connection.json"
    path.write_text(login.model_dump_json())
    return path


def test_connection_file_import_rebinds_the_account_and_prints_the_address(auth_env):
    path = connection_file(auth_env)
    extra = ["--auth-config", path, "--name", "kc", "--yes", "--flow", "browser", "--no-browser", "--skip-workspace"]
    result = auth_env.invoke(*setup_args(auth_env, *extra, "--output", "json", server=None))
    assert result.exit_code == 0, result.stderr
    value = one_json(result)
    assert value["profile"] == "kc" and value["authenticated"] is True and value["workspace"] is None
    profile = auth_env.store().get("kc")
    assert profile.source == "file" and profile.login.account == "kc" and profile.provider_name is None
    # A connection file says nothing about the administrator's flows: both stay allowed.
    assert profile.flows == ["browser", "device"]
    assert auth_env.platform.opened == [False]
    assert "Open this address in a browser on this computer to sign in:" in result.stderr
    assert auth_env.platform.authorizations[0] in result.stderr
    assert "Connection file:" in result.stderr and auth_env.platform.requests[0][1].startswith(ISSUER)


def test_connection_file_must_match_server_and_be_valid(auth_env):
    path = connection_file(auth_env)
    other = auth_env.invoke(
        *setup_args(auth_env, "--auth-config", path, "--yes", "--output", "json", server="x.example")
    )
    assert other.exit_code == 2 and one_json(other)["flag"] == "SERVER"
    path.write_text('{"issuer": "do-not-echo"}')
    broken = auth_env.invoke(*setup_args(auth_env, "--auth-config", path, "--yes", "--output", "json", server=None))
    assert broken.exit_code == 2 and one_json(broken)["code"] == "WV-AUTH-CONFIG"
    assert "do-not-echo" not in broken.stdout + broken.stderr


@pytest.mark.parametrize(
    "target, detail",
    [
        ("https://169.254.169.254", "WV-CONNECT-BLOCKED"),
        ("https://WEAVE.example", None),
        ("https://weave.example:443", None),
    ],
)
def test_connection_file_server_follows_the_address_rules(auth_env, target, detail):
    from firefly_weave.sdk.auth import LoginConfig

    login = LoginConfig(
        provider_id="acme", issuer=ISSUER, client_id="weave-cli", target=target, account="ops", scopes=("openid",)
    )
    path = auth_env.root / "connection.json"
    path.write_text(login.model_dump_json())
    result = auth_env.invoke(*setup_args(auth_env, "--auth-config", path, "--yes", "--output", "json", server=None))
    assert result.exit_code == 2, (result.stdout, result.stderr)
    value = one_json(result)
    assert value["code"] == "WV-AUTH-CONFIG" and value["detail"] == detail and value["saved"] is False
    assert not auth_env.profiles_file.exists() and auth_env.platform.requests == []


def test_replacing_with_the_same_credentials_keeps_the_sign_in(auth_env):
    first = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", "device", "--skip-workspace", "--name", "prod"))
    assert first.exit_code == 0, first.stderr
    old = auth_env.store().get("prod")
    again = auth_env.invoke(
        *setup_args(auth_env, "--yes", "--no-login", "--name", "prod", "--replace", "--output", "json")
    )
    assert again.exit_code == 0, again.stderr
    value = one_json(again)
    assert value["authenticated"] is True and value["account"]["subject"] == "subject-1"
    assert value["next"] == ["weave auth workspace --profile prod"]
    assert "You are still signed in." in again.stderr and auth_env.record(old).state == "active"
    moved = auth_env.invoke(
        "auth", "setup", "weave.example", "--yes", "--no-login", "--name", "prod", "--replace",
        "--credential-store", "file", "--credential-file", auth_env.credential_file("other.json"), "--output", "json",
    )  # fmt: skip
    assert moved.exit_code == 0, moved.stderr
    assert one_json(moved)["authenticated"] is False and one_json(moved)["account"] is None
    assert auth_env.record(old) is None, "Credentials left in the replaced store are removed"


def test_file_store_is_refused_where_it_is_unsupported(auth_env, monkeypatch):
    from types import SimpleNamespace

    from firefly_weave.cli import auth_setup

    monkeypatch.setattr(auth_setup, "os", SimpleNamespace(name="nt"))
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--output", "json"))
    assert result.exit_code == 2 and one_json(result)["code"] == "WV-AUTH-STORE", (result.stdout, result.exception)
    assert "macOS and Linux only" in result.stderr and not auth_env.profiles_file.exists()


# --- Ctrl+C ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answers, saved, authenticated",
    [
        ([KeyboardInterrupt], False, False),  # step 1: server address
        (["weave.example", click.Abort()], False, False),  # step 2: sign-in option
        (["weave.example", "1", KeyboardInterrupt], False, False),  # step 2: profile name
        (["weave.example", "1", "", click.Abort()], False, False),  # step 2: trust
        (["weave.example", "1", "", True, KeyboardInterrupt], True, True),  # step 4: workspace picker
    ],
)
def test_ctrl_c_at_each_question_reports_the_partial_state(auth_env, answers, saved, authenticated):
    auth_env.platform.sign_in = [option(), option(provider_id="partner", display_name="Partner")]
    console = ScriptedConsole("text", answers)
    result = auth_env.invoke(*setup_args(auth_env, server=None), console=lambda output: console)
    assert result.exit_code == 1, (result.stderr, result.exception)
    assert result.stderr.splitlines()[-3:][0] == "Cancelled." or "\nCancelled.\n" in result.stderr
    assert "Traceback" not in result.stderr and not result.stdout
    assert auth_env.profiles_file.exists() is saved
    if not saved:
        assert "Nothing was saved." in result.stderr
    else:
        assert "is saved and you are signed in, but no workspace was chosen" in result.stderr
        profile = auth_env.store().get("acme-weave")
        assert profile.workspace is None and auth_env.record(profile).state == "active"


@pytest.mark.parametrize("output", ["text", "json"])
def test_ctrl_c_while_signing_in_keeps_the_profile_and_signs_nothing_in(auth_env, output):
    auth_env.platform.browser_error = KeyboardInterrupt()
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", "browser", "--output", output))
    assert result.exit_code == 1, (result.stdout, result.stderr, result.exception)
    assert "Cancelled." in result.stderr and "Traceback" not in result.stderr
    assert "is saved, but you are not signed in" in result.stderr
    profile = auth_env.store().get("acme-weave")
    assert auth_env.record(profile).state == "logged_out"
    if output == "json":
        assert one_json(result) == {
            "code": "WV-AUTH-CANCELLED",
            "message": "Cancelled.",
            "profile": "acme-weave",
            "server": SERVER,
            "saved": True,
            "authenticated": False,
            "workspace": None,
        }
    else:
        assert not result.stdout


def test_denied_sign_in_reports_saved_but_not_signed_in(auth_env):
    import httpx

    original = auth_env.platform.handler

    def deny(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(400, json={"error": "access_denied"})
        return original(request)

    auth_env.monkeypatch.setattr(auth_env.platform, "handler", deny)
    result = auth_env.invoke(*setup_args(auth_env, "--yes", "--flow", "device", "--output", "json"))
    assert result.exit_code == 1
    value = one_json(result)
    assert value["code"] == "WV-AUTH-DENIED" and value["saved"] is True and value["authenticated"] is False


# --- real terminals ----------------------------------------------------------------------------------------------


def cli_environment(tmp_path):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("WEAVE_") and key not in {"SSH_CONNECTION", "SSH_TTY"}
    }
    tmp_path.chmod(0o700)
    environment.update(PYTHONPATH=str(SRC), WEAVE_CONFIG_HOME=str(tmp_path / "config"), WEAVE_NO_ANIMATION="1")
    return environment


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_ctrl_c_in_a_real_terminal_cancels_cleanly(tmp_path):
    import fcntl
    import termios

    controller, terminal = os.openpty()

    def controlling_terminal():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(
        [sys.executable, "-m", "firefly_weave.cli.main", "auth", "setup"],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        env=cli_environment(tmp_path),
        start_new_session=True,
        preexec_fn=controlling_terminal,
        close_fds=True,
    )
    os.close(terminal)
    output = b""
    try:
        deadline = time.monotonic() + 30
        while b"Server address" not in output:
            assert time.monotonic() < deadline, output
            ready, _, _ = select.select([controller], [], [], 1)
            if ready:
                output += os.read(controller, 4096)
        os.write(controller, b"\x03")
        deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < deadline, output
            ready, _, _ = select.select([controller], [], [], 1)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(controller, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        assert process.wait(10) == 1, output
    finally:
        if process.poll() is None:
            process.kill()
        os.close(controller)
    text = output.decode(errors="replace")
    assert "Step 1 of 4" in text and "Cancelled." in text and "Nothing was saved." in text
    assert "Traceback" not in text and "Aborted!" not in text
    assert not (tmp_path / "config" / "profiles.json").exists()


@pytest.mark.parametrize("output", ["text", "json"])
def test_without_a_terminal_setup_never_prompts(tmp_path, output):
    result = subprocess.run(
        [sys.executable, "-m", "firefly_weave.cli.main", "auth", "setup", "--output", output],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=cli_environment(tmp_path),
        timeout=60,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Server address" not in result.stderr and "Traceback" not in result.stderr
    assert "SERVER" in result.stderr
    if output == "json":
        assert json.loads(result.stdout)["code"] == "WV-AUTH-INPUT"
    else:
        assert result.stdout == ""


def test_switch_account_url_carries_prompt_login(auth_env):
    path = connection_file(auth_env)
    extra = ["--auth-config", path, "--name", "kc", "--yes", "--skip-workspace"]
    assert auth_env.invoke(*setup_args(auth_env, *extra, server=None)).exit_code == 0
    result = auth_env.invoke("auth", "login", "--profile", "kc", "--switch-account", "--output", "json")
    assert result.exit_code == 0, result.stderr
    query = parse_qs(urlsplit(auth_env.platform.authorizations[-1]).query)
    assert query["prompt"] == ["login"]
