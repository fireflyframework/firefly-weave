# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""CLI for no-code integrations: import-openapi modes, http-action, descriptor and guided connections."""

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from auth_support import SERVER, auth_env_fixture  # noqa: F401
from click.testing import CliRunner

from firefly_weave.cli.connectors import classify
from firefly_weave.cli.main import cli
from firefly_weave.contracts.connectors import ConnectionRevision, ConnectionTestResult
from firefly_weave.sdk import client

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests/fixtures/openapi/petstore.yaml"
T, P, E = UUID(int=1), UUID(int=2), UUID(int=3)
VERSION_ID = UUID(int=42)
RELAX = ["--numeric-formats", "--ignore-response-headers", "--json-media-only", "--default-string-max-length", "64"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(tmp_path / "config"))
    for name in (
        "WEAVE_BASE_URL",
        "WEAVE_PROFILE",
        "WEAVE_TENANT_ID",
        "WEAVE_PROJECT_ID",
        "WEAVE_ENVIRONMENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "environment-token")


def run(*args, input=None):
    return CliRunner().invoke(cli, [str(a) for a in args], input=input)


def one_json(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def platform(monkeypatch):
    """A fake SDK client: records calls and answers like a project with weave-http@2.0.0 published."""
    calls = []
    targets = []
    state = {"published": True}

    class Fake:
        def __init__(self, base_url, provider, scope, **kwargs):
            targets.append((base_url, scope))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, **kwargs):
            calls.append((operation, kwargs))
            if operation == "definitions.list":
                items = [SimpleNamespace(name="other", version="1.0.0", id=UUID(int=7))]
                if state["published"]:
                    items.append(SimpleNamespace(name="weave-http", version="2.0.0", id=VERSION_ID))
                return SimpleNamespace(items=items, next_cursor=None)
            return {"operation": operation}

        async def read_connector_descriptor(self, adapter):
            from firefly_weave.contracts.connector_descriptors import descriptor_view
            from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

            calls.append(("connector_descriptors.read", {"adapter": adapter}))
            return descriptor_view(HTTP_PROFILE_DESCRIPTOR, VERSION_ID)

    monkeypatch.setattr(client, "WeaveClient", Fake)
    return SimpleNamespace(calls=calls, state=state, targets=targets)


EXPLICIT = ["--base-url", "https://weave.example.test", "--tenant", T, "--project", P, "--environment", E]


def test_list_reports_verdicts_in_json_and_text():
    result = run("connector", "import-openapi", FIXTURE, "--list", "--output", "json")
    assert result.exit_code == 0, result.output
    data = one_json(result)
    rows = {op["key"]: op for op in data["operations"]}
    assert rows["createPet"]["supported"] and not rows["showPetById"]["supported"]
    text = run("connector", "import-openapi", FIXTURE, "--list", *RELAX)
    assert text.exit_code == 0
    assert "4 of 5 operations import as is." in text.output
    assert "WV-IMPORT-OPERATION_ID" in text.output


def test_list_reads_standard_input_and_rejects_bad_documents():
    legacy = json.dumps({"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "paths": {}})
    result = run("connector", "import-openapi", "-", "--list", "--output", "json", input=legacy)
    assert result.exit_code == 1 and one_json(result)["diagnostics"][0]["code"] == "WV-IMPORT-VERSION"
    upgraded = run(
        "connector", "import-openapi", "-", "--list", "--upgrade-openapi-30", "--output", "json", input=legacy
    )
    assert upgraded.exit_code == 0 and one_json(upgraded)["ok"]


def test_init_policy_writes_once_and_imports_builtin_actions(tmp_path):
    policy = tmp_path / "policy.json"
    result = run("connector", "import-openapi", FIXTURE, "--init-policy", policy, *RELAX)
    assert result.exit_code == 0, result.output
    rules = json.loads(policy.read_text())
    assert rules["relaxations"]["defaultStringMaxLength"] == 64 and len(rules["operations"]) == 4
    again = run("connector", "import-openapi", FIXTURE, "--init-policy", policy)
    assert again.exit_code == 1 and json.loads(policy.read_text()) == rules
    out = tmp_path / "pets"
    result = run("connector", "import-openapi", FIXTURE, "--policy", policy, "--target", "builtin", "--directory", out)
    assert result.exit_code == 0, result.output
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "actions/create-pet.action.json",
        "actions/delete-pet.action.json",
        "actions/list-pets.action.json",
        "actions/show-pet-by-id.action.json",
        "connection.example.json",
        "provenance.json",
    ]
    action = json.loads((out / "actions/create-pet.action.json").read_text())
    assert action["spec"]["implementation"]["uses"] == "weave-http@2.0.0"
    assert json.loads((out / "connection.example.json").read_text())["secretRef"] == {
        "api_key": "<operator secret handle for api_key>"
    }
    assert "warning WV-IMPORT-RELAXED_MEDIA" in result.output


def test_import_collects_every_diagnostic_on_request(tmp_path):
    policy = tmp_path / "policy.json"
    assert run("connector", "import-openapi", FIXTURE, "--init-policy", policy, *RELAX).exit_code == 0
    strict = json.loads(policy.read_text())
    strict.pop("relaxations")
    policy.write_text(json.dumps(strict))
    first = run("connector", "import-openapi", FIXTURE, "--policy", policy, "--target", "builtin", "--output", "json")
    assert first.exit_code == 1 and len(one_json(first)["diagnostics"]) == 1
    every = run(
        "connector",
        "import-openapi",
        FIXTURE,
        "--policy",
        policy,
        "--target",
        "builtin",
        "--all-diagnostics",
    )
    assert every.exit_code == 1
    assert every.output.count("error WV-") == 4 and "hint: " in every.output
    relaxed_by_flags = run(
        "connector", "import-openapi", FIXTURE, "--policy", policy, "--target", "builtin", *RELAX, "--output", "json"
    )
    assert relaxed_by_flags.exit_code == 0 and len(one_json(relaxed_by_flags)["actions"]) == 4


@pytest.mark.parametrize(
    "args",
    [
        ["--list", "--init-policy", "p.json"],
        ["--list", "--directory", "out"],
        [],
    ],
)
def test_import_usage_errors(args):
    result = run("connector", "import-openapi", FIXTURE, *args)
    assert result.exit_code == 2 and "WV-CLI-USAGE" in result.output


def test_http_action_builds_checks_and_writes(tmp_path):
    sample = tmp_path / "pet.json"
    sample.write_text(json.dumps({"id": 1, "name": "SAMPLE-CANARY"}))
    result = run(
        "connector",
        "http-action",
        "--name",
        "get-pet",
        "--method",
        "get",
        "--path",
        "/v1/pets/{petId}",
        "--param",
        "path:petId:string",
        "--param",
        "query:limit:integer",
        "--param",
        "query:tag:string[]:required",
        "--status",
        "200",
        "--response-sample",
        sample,
        "--output-dir",
        tmp_path / "actions",
        "--output",
        "json",
    )
    assert result.exit_code == 0, result.output
    data = one_json(result)
    assert data["ok"] and data["compiled"] and "SAMPLE-CANARY" not in result.output
    written = json.loads((tmp_path / "actions/get-pet.action.json").read_text())
    assert written == data["action"]
    query = written["spec"]["inputSchema"]["properties"]["query"]
    assert query["required"] == ["tag"] and query["properties"]["tag"]["type"] == "array"
    again = run(
        "connector",
        "http-action",
        "--name",
        "get-pet",
        "--method",
        "GET",
        "--path",
        "/v1/pets/{petId}",
        "--output-dir",
        tmp_path / "actions",
    )
    assert again.exit_code == 1 and "WV-HTTP-ACTION-IO" in again.output


def test_http_action_rejects_protected_headers_in_text_mode():
    result = run(
        "connector",
        "http-action",
        "--name",
        "x",
        "--method",
        "POST",
        "--path",
        "/v1/x",
        "--param",
        "header:Authorization",
    )
    assert result.exit_code == 1
    assert "WV-HTTP-ACTION-PROTECTED_HEADER" in result.output and "hint:" in result.output
    bad = run("connector", "http-action", "--name", "x", "--method", "GET", "--path", "/x", "--param", "cookie:a")
    assert bad.exit_code == 2


def test_descriptor_local_copy_without_a_platform(platform):
    result = run("connector", "descriptor", "weave-http-v2")
    assert result.exit_code == 0, result.output
    data = one_json(result)
    assert data["provenance"] == "local-copy" and "authoritative" in data["note"]
    assert json.loads(data["source"]) == data["manifest"]
    assert data["reference"] == "weave-http@2.0.0" and data["manifest"]["spec"]["adapter"] == "weave-http-v2"
    assert not platform.calls
    missing = run("connector", "descriptor", "weave-kafka")
    assert missing.exit_code == 2 and "No platform is selected" in missing.output
    unknown = run("connector", "descriptor", "weave-kafka", "--local")
    assert unknown.exit_code == 1 and "WV-CONNECTOR-DESCRIPTOR" in unknown.output


def test_descriptor_reads_the_installed_copy_from_the_platform(platform):
    result = run("connector", "descriptor", "weave-http-v2", *EXPLICIT)
    assert result.exit_code == 0, result.output
    data = one_json(result)
    assert data["provenance"] == "platform" and data["published_version_id"] == str(VERSION_ID)
    assert platform.calls == [("connector_descriptors.read", {"adapter": "weave-http-v2"})]
    text = run("connector", "descriptor", "weave-http-v2", *EXPLICIT, "--output", "text")
    assert "weave-http@2.0.0" in text.output and "Published as connector version" in text.output


def test_guided_connection_resolves_the_version_and_sends_handles_only(platform):
    result = run(
        "connections",
        "create",
        *EXPLICIT,
        "--name",
        "pets",
        "--api-url",
        "https://api.example.com",
        "--auth",
        "api-key",
        "--auth-header",
        "X-API-Key",
        "--secret",
        "api_key=pets-api-key",
    )
    assert result.exit_code == 0, result.output
    assert [call[0] for call in platform.calls] == ["definitions.list", "connections.create"]
    assert platform.calls[0][1]["collection"] == "connectors"
    body = platform.calls[1][1]["body"].model_dump(by_alias=True, mode="json")
    assert body == {
        "name": "pets",
        "connector_version_id": str(VERSION_ID),
        "config": {"baseUrl": "https://api.example.com", "auth": {"kind": "api-key", "header": "X-API-Key"}},
        "secretRef": {"api_key": "pets-api-key"},
        "allowed_destinations": ["https://api.example.com"],
    }


def test_guided_connection_uses_the_saved_profile_target(auth_env, platform):
    from firefly_weave.sdk.profiles import WorkspaceSelection

    saved = WorkspaceSelection(tenant_id=T, project_id=P, environment_id=E, tenant_name="Acme")
    auth_env.save_profile(workspace=saved)
    result = auth_env.invoke("connections", "create", "--name", "pets", "--api-url", "https://api.example.com")
    assert result.exit_code == 0, result.output
    (base_url, scope), *_ = platform.targets
    assert base_url == SERVER and (scope.tenant_id, scope.project_id, scope.environment_id) == (T, P, E)
    descriptor = auth_env.invoke("connector", "descriptor", "weave-http-v2")
    assert descriptor.exit_code == 0 and one_json(descriptor)["provenance"] == "platform"


def test_guided_connection_checks_locally_before_any_call(platform):
    result = run("connections", "create", *EXPLICIT, "--name", "pets", "--api-url", "http://api.example.com/v1")
    assert result.exit_code == 2 and one_json(result)["code"] == "WV-CONNECTION-INPUT"
    assert one_json(result)["diagnostics"][0]["path"] == "/config/baseUrl"
    assert not platform.calls
    platform.state["published"] = False
    missing = run("connections", "create", *EXPLICIT, "--name", "pets", "--api-url", "https://api.example.com")
    assert missing.exit_code == 1 and one_json(missing)["code"] == "WV-CONNECTION-CONNECTOR"
    assert [call[0] for call in platform.calls] == ["definitions.list"]


def test_raw_request_path_still_works_and_cannot_mix_with_guided_flags(platform, tmp_path):
    request = tmp_path / "c.json"
    request.write_text(
        json.dumps(
            {
                "name": "pets",
                "connector_version_id": str(VERSION_ID),
                "config": {"baseUrl": "https://api.example.com", "auth": {"kind": "none"}},
                "allowed_destinations": ["https://api.example.com"],
            }
        )
    )
    result = run("connections", "create", *EXPLICIT, "--request", request)
    assert result.exit_code == 0, result.output
    assert [call[0] for call in platform.calls] == ["connections.create"]
    mixed = run("connections", "create", *EXPLICIT, "--request", request, "--name", "pets")
    assert mixed.exit_code == 2 and one_json(mixed)["code"] == "WV-CLI-USAGE"
    neither = run("connections", "create", *EXPLICIT)
    assert neither.exit_code == 2


def test_connection_test_needs_no_request_file(platform):
    result = run("connections", "test", UUID(int=5), *EXPLICIT)
    assert result.exit_code == 0, result.output
    operation, kwargs = platform.calls[-1]
    assert operation == "connections.test" and kwargs["identifier"] == UUID(int=5)
    assert kwargs["body"].model_dump() == {}


def test_connector_failures_are_classified_without_echoing_values(tmp_path):
    secret = "VALUE-CANARY"
    assert classify(ValueError(f"bad {secret}")) == (
        "WV-CONNECTOR-INVALID",
        "The declaration or its inputs are invalid; check names, versions and schemas.",
    )
    assert classify(ValueError("Scaffold target must be absent or an empty directory"))[1].startswith("Scaffold")
    assert classify(subprocess.CalledProcessError(1, ["build"]))[0] == "WV-CONNECTOR-BUILD"
    assert classify(ModuleNotFoundError("build"))[0] == "WV-CONNECTOR-DEPENDENCY"
    broken = tmp_path / "connector.json"
    broken.write_text('{"format": "' + secret + '"}')
    result = run("connector", "validate", broken, "--output", "json")
    assert result.exit_code == 1 and secret not in result.output
    assert "WV-CONNECTOR-INVALID" in result.output
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep/README.md").write_text("mine")
    result = run("connector", "init", tmp_path / "keep", "--name", "acme-echo")
    assert result.exit_code == 1 and "absent or an empty directory" in result.output


def test_group_help_lists_the_no_code_commands():
    result = run("connector", "--help")
    assert result.exit_code == 0
    for name in ("import-openapi", "http-action", "descriptor"):
        assert name in result.output


def test_template_pin_follows_the_project_version(tmp_path):
    from firefly_weave import __version__
    from firefly_weave.sdk.connectors import scaffold

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == project
    scaffold(tmp_path / "echo", "acme-echo")
    text = (tmp_path / "echo/pyproject.toml").read_text()
    assert f'"firefly-weave[server,client]=={project}"' in text and "__" not in text.split("[project.entry")[0]


@pytest.mark.parametrize(
    "content",
    [None, b"\xef\xbb\xbf{}", b'1, "value": {"injected": true}', b"{" * 5],
    ids=["missing", "bom", "wrapper-injection", "malformed"],
)
@pytest.mark.parametrize("output", ["json", "text"])
def test_http_action_reports_unreadable_samples_without_crashing(tmp_path, content, output):
    sample = tmp_path / "sample.json"
    if content is not None:
        sample.write_bytes(content)
    result = run(
        "connector",
        "http-action",
        "--name",
        "get-pet",
        "--method",
        "GET",
        "--path",
        "/v1/pets",
        "--response-sample",
        sample,
        "--output",
        output,
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1 and "WV-HTTP-ACTION-IO" in result.output
    assert "injected" not in result.output


def test_relaxation_flags_with_a_malformed_policy_fail_cleanly(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"name": "pets", "relaxations": "everything"}))
    result = run("connector", "import-openapi", FIXTURE, "--policy", policy, "--numeric-formats", "--output", "json")
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1 and one_json(result)["diagnostics"][0]["code"] == "WV-IMPORT-POLICY"


def test_guided_machine_token_connection_allows_the_token_endpoint(platform):
    result = run(
        "connections",
        "create",
        *EXPLICIT,
        "--name",
        "pets",
        "--api-url",
        "https://api.example.com",
        "--auth",
        "machine-token",
        "--client-id",
        "weave",
        "--token-endpoint",
        "https://login.example.com/oauth/token",
        "--scope",
        "pets.read",
        "--secret",
        "client_secret=pets-client",
        "--connector-version-id",
        VERSION_ID,
    )
    assert result.exit_code == 0, result.output
    assert [call[0] for call in platform.calls] == ["connections.create"]
    body = platform.calls[0][1]["body"].model_dump(by_alias=True, mode="json")
    assert body["allowed_destinations"] == ["https://api.example.com", "https://login.example.com"]
    assert body["config"]["auth"] == {
        "kind": "machine-token",
        "client_id": "weave",
        "endpoint": "https://login.example.com/oauth/token",
        "scopes": ["pets.read"],
    }
    assert body["secretRef"] == {"client_secret": "pets-client"}


PLAIN = "http://api.example.com"
NOT_ENCRYPTED = "Not encrypted: requests to http://api.example.com travel in plain text."


def test_guided_plain_http_connection_is_created_with_a_warning(platform):
    result = run(
        "connections",
        "create",
        *EXPLICIT,
        "--name",
        "pets",
        "--api-url",
        PLAIN,
        "--auth",
        "bearer",
        "--secret",
        "token=pets-token",
    )
    assert result.exit_code == 0, result.output
    body = platform.calls[1][1]["body"].model_dump(by_alias=True, mode="json")
    assert body["config"]["baseUrl"] == PLAIN and body["allowed_destinations"] == [PLAIN]
    # Standard output stays one JSON result; the warning is one line on standard error.
    assert one_json(result) == {"operation": "connections.create"}
    assert NOT_ENCRYPTED in result.stderr.splitlines()
    secure = run("connections", "create", *EXPLICIT, "--name", "pets", "--api-url", "https://api.example.com")
    assert secure.exit_code == 0 and "Not encrypted" not in secure.stderr


@pytest.fixture
def plain_connection(monkeypatch):
    """A fake SDK client whose connection is plain HTTP: reads return it, tests answer encrypted: false."""
    calls = []
    revision = ConnectionRevision(
        id=UUID(int=11),
        revision=1,
        name="pets",
        connector_version_id=VERSION_ID,
        connector="weave-http@2.0.0",
        connector_digest="a" * 64,
        adapter="weave-http-v2",
        config={"baseUrl": PLAIN, "auth": {"kind": "none"}},
        allowed_destinations=(PLAIN,),
    )

    class Fake:
        def __init__(self, base_url, provider, scope, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, **kwargs):
            calls.append(operation)
            return ConnectionTestResult(ok=True, encrypted=False) if operation == "connections.test" else revision

    monkeypatch.setattr(client, "WeaveClient", Fake)
    return calls


def test_connection_test_and_read_warn_for_plain_http(plain_connection):
    tested = run("connections", "test", UUID(int=11), *EXPLICIT)
    assert tested.exit_code == 0, tested.output
    assert one_json(tested)["encrypted"] is False
    assert NOT_ENCRYPTED in tested.stderr.splitlines()
    # The test answer carries only the flag; the CLI reads the revision for the origin it names.
    assert plain_connection == ["connections.test", "connections.read"]
    read = run("connections", "read", UUID(int=11), *EXPLICIT)
    assert read.exit_code == 0, read.output
    assert one_json(read)["config"]["baseUrl"] == PLAIN
    assert NOT_ENCRYPTED in read.stderr.splitlines()


def test_builtin_import_of_a_plain_http_server_warns(tmp_path):
    document = tmp_path / "petstore.yaml"
    document.write_text(FIXTURE.read_text().replace("https://api.petstore.test", "http://api.petstore.test"))
    policy = tmp_path / "policy.json"
    scaffold = run("connector", "import-openapi", document, "--init-policy", policy, *RELAX)
    assert scaffold.exit_code == 0, scaffold.output
    warning = "Not encrypted: requests to http://api.petstore.test travel in plain text."
    text = run("connector", "import-openapi", document, "--policy", policy, "--target", "builtin")
    assert text.exit_code == 0, text.output
    # Standard output carries the import summary; the warning is one line on standard error.
    assert warning in text.stderr.splitlines() and "Not encrypted" not in text.stdout
    as_json = run(
        "connector", "import-openapi", document, "--policy", policy, "--target", "builtin", "--output", "json"
    )
    assert as_json.exit_code == 0, as_json.output
    assert one_json(as_json)["connectionExample"]["config"]["baseUrl"] == "http://api.petstore.test"
    assert warning in as_json.stderr.splitlines()
