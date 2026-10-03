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

"""Server address screening, public client configuration errors and provider probes (simulated HTTP)."""

import asyncio
import json
import ssl

import httpx
import pytest

from firefly_weave.contracts.client_configuration import ClientConfiguration, SignInOption
from firefly_weave.sdk.auth import LoginConfig
from firefly_weave.sdk.profiles import ProfileError
from firefly_weave.sdk.server_config import (
    ProviderCheck,
    ServerConfigError,
    fetch_client_configuration,
    issuer_origin,
    login_config_for,
    normalize_server_address,
    probe_sign_in,
    require_sign_in,
)

OPTION = {
    "provider_id": "acme",
    "display_name": "Acme (Keycloak)",
    "issuer": "https://login.example/realms/acme",
    "client_id": "weave-cli",
    "scopes": ["openid"],
    "trusted_endpoint_origins": ["https://device.example"],
    "allow_loopback_http": False,
    "flows": ["browser", "device"],
    "require_refresh_rotation": True,
}
DOCUMENT = {
    "service": "firefly-weave",
    "configuration_version": 1,
    "api_version": "weave/api-v1",
    "display_name": "Acme Weave",
    "sign_in": [OPTION],
}


def code_of(failure):
    return failure.value.code


# --- address normalization ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("weave.example", "https://weave.example"),
        ("  HTTPS://Weave.Example/ ", "https://weave.example"),
        ("https://weave.example:8443", "https://weave.example:8443"),
        ("https://weave.example:443", "https://weave.example"),
        ("weave.example:", "https://weave.example"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
        ("localhost:8000", "https://localhost:8000"),
        ("10.0.0.5", "https://10.0.0.5"),
        ("https://192.168.1.20:9443", "https://192.168.1.20:9443"),
        ("https://[fd00::1]", "https://[fd00::1]"),
        ("weave_internal.corp.example", "https://weave_internal.corp.example"),
        ("bücher.example", "https://xn--bcher-kva.example"),
        ("https://[64:ff9b::808:808]", "https://[64:ff9b::808:808]"),
    ],
)
def test_server_addresses_are_normalized(text, expected):
    assert normalize_server_address(text) == expected
    assert normalize_server_address(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://",
        "https://user@weave.example",
        "https://user:secret@weave.example",
        "https://weave.example/path",
        "https://weave.example//",
        "https://weave.example?x=1",
        "https://weave.example?",
        "https://weave.example#fragment",
        "https://weave.example:0",
        "https://weave.example:99999",
        "https://weave.example:abc",
        "ftp://weave.example",
        "https://wea ve.example",
        "https://weave.ex\nample",
        "https://weave.example\x7f",
        "https://weave..example",
        "https://weave.example.",
        "https://-weave.example",
        "https://weave.example\\evil",
        "https://<script>",
        "https://2852039166",
        "https://169.254.43518",
        "https://0x7f.1",
        "https://[fe80::1%25en0]",
        "https://[v1.weave]",
        "[v1.fe]:8443",
        None,
    ],
)
def test_malformed_addresses_are_rejected(text):
    with pytest.raises(ServerConfigError) as failure:
        normalize_server_address(text)
    assert code_of(failure) == "WV-CONNECT-ADDRESS" and failure.value.exit_code == 2


@pytest.mark.parametrize(
    "text",
    [
        "169.254.169.254",
        "http://169.254.169.254",
        "https://[fe80::1]",
        "https://0.0.0.0",
        "https://[::]",
        "https://224.0.0.1",
        "https://[ff02::1]",
        "https://[::ffff:169.254.169.254]",
        "https://255.255.255.255",
        "https://[fd00:ec2::254]",
        "https://[64:ff9b::a9fe:a9fe]",
        "https://[64:ff9b::e000:1]",
        "https://[2002:a9fe:a9fe::1]",
        "metadata.google.internal",
    ],
)
def test_link_local_metadata_and_unspecified_targets_are_blocked(text):
    with pytest.raises(ServerConfigError) as failure:
        normalize_server_address(text)
    assert code_of(failure) == "WV-CONNECT-BLOCKED"


@pytest.mark.parametrize(
    "text", ["http://weave.example", "http://10.0.0.5", "http://127.0.0.2", "http://localhost.evil"]
)
def test_plain_http_is_loopback_only(text):
    with pytest.raises(ServerConfigError) as failure:
        normalize_server_address(text)
    assert code_of(failure) == "WV-CONNECT-INSECURE"


# --- client configuration document -------------------------------------------------------------------------------


def serve(handler):
    calls = []

    async def receive(request):
        calls.append(request)
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    return httpx.MockTransport(receive), calls


async def test_configuration_is_read_from_the_fixed_public_path_without_credentials():
    transport, calls = serve(lambda request: httpx.Response(200, json=DOCUMENT))
    configuration = await fetch_client_configuration("Weave.Example/", transport=transport)
    assert isinstance(configuration, ClientConfiguration)
    assert configuration.display_name == "Acme Weave" and configuration.sign_in[0].provider_id == "acme"
    (request,) = calls
    assert request.method == "GET" and str(request.url) == "https://weave.example/api/v1/client-configuration"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert request.headers["accept-encoding"] == "identity" and request.headers["accept"] == "application/json"
    assert require_sign_in(configuration) == configuration.sign_in


async def test_empty_sign_in_list_is_reported_by_callers():
    transport, _ = serve(lambda request: httpx.Response(200, json={**DOCUMENT, "sign_in": []}))
    configuration = await fetch_client_configuration("https://weave.example", transport=transport)
    with pytest.raises(ServerConfigError) as failure:
        require_sign_in(configuration)
    assert code_of(failure) == "WV-CONNECT-NO-SIGN-IN" and failure.value.exit_code == 3


@pytest.mark.parametrize(
    "location, detail",
    [
        ("https://login.example/start?next=/x", "https://login.example"),
        ("/elsewhere", "https://weave.example"),
        ("http://weave.example/api", None),
        ("https://169.254.169.254/", None),
        (None, None),
    ],
)
async def test_redirects_are_never_followed_and_report_only_an_https_origin(location, detail):
    headers = {"location": location} if location else {}
    transport, calls = serve(lambda request: httpx.Response(302, headers=headers))
    with pytest.raises(ServerConfigError) as failure:
        await fetch_client_configuration("https://weave.example", transport=transport)
    assert code_of(failure) == "WV-CONNECT-REDIRECT" and failure.value.detail == detail
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(404, text="Not found"), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(403, json={"error": "forbidden"}), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(500, text="boom"), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(503, text="maintenance"), "WV-CONNECT-UNREACHABLE"),
        (httpx.Response(502, text="bad gateway"), "WV-CONNECT-UNREACHABLE"),
        (
            httpx.Response(401, json={"code": "WV-UNAUTHENTICATED"}, headers={"X-Weave-Wire-Version": "weave/api-v1"}),
            "WV-CONNECT-INCOMPATIBLE",
        ),
        (
            httpx.Response(404, json={"code": "WV-NOT-FOUND"}, headers={"X-Weave-Wire-Version": "weave/api-v1"}),
            "WV-CONNECT-INCOMPATIBLE",
        ),
        (
            httpx.Response(200, json=DOCUMENT, headers={"X-Weave-Wire-Version": "weave/api-v2"}),
            "WV-CONNECT-INCOMPATIBLE",
        ),
        (httpx.Response(200, html="<html><body>Welcome</body></html>"), "WV-CONNECT-NOT-WEAVE"),
        (
            httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
            "WV-CONNECT-NOT-WEAVE",
        ),
        (httpx.Response(200, json=[DOCUMENT]), "WV-CONNECT-NOT-WEAVE"),
        (
            httpx.Response(200, content=b'{"service":' + b"[" * 30000, headers={"content-type": "application/json"}),
            "WV-CONNECT-NOT-WEAVE",
        ),
        (httpx.Response(200, json={"status": "ok"}), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(200, json={**DOCUMENT, "sign_in": "everyone"}), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(200, json={**DOCUMENT, "unexpected": True}), "WV-CONNECT-NOT-WEAVE"),
        (httpx.Response(200, json={**DOCUMENT, "service": "other-product"}), "WV-CONNECT-INCOMPATIBLE"),
        (httpx.Response(200, json={**DOCUMENT, "configuration_version": 2}), "WV-CONNECT-INCOMPATIBLE"),
        (httpx.Response(200, json={**DOCUMENT, "api_version": "weave/api-v2"}), "WV-CONNECT-INCOMPATIBLE"),
        (
            httpx.Response(
                200,
                stream=httpx.ByteStream(json.dumps(DOCUMENT).encode()),
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            ),
            "WV-CONNECT-NOT-WEAVE",
        ),
        (
            httpx.Response(
                200,
                content=b'{"service":"firefly-weave","pad":"' + b"x" * 70000 + b'"}',
                headers={"content-type": "application/json"},
            ),
            "WV-CONNECT-NOT-WEAVE",
        ),
    ],
)
async def test_responses_map_to_stable_codes_without_echoing_bodies(response, expected):
    transport, _ = serve(lambda request: response)
    with pytest.raises(ServerConfigError) as failure:
        await fetch_client_configuration("https://weave.example", transport=transport)
    assert code_of(failure) == expected and failure.value.exit_code == 3
    for leaked in ("boom", "Welcome", "maintenance", "forbidden", "other-product"):
        assert leaked not in str(failure.value) and leaked not in str(failure.value.detail)


def tls_failure(request):
    try:
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate")
    except ssl.SSLError as error:
        try:
            raise OSError("handshake failed") from error
        except OSError as wrapped:
            raise httpx.ConnectError("connect failed") from wrapped


def raises(error):
    def handler(request):
        raise error

    return handler


@pytest.mark.parametrize(
    "handler, expected",
    [
        (raises(httpx.ConnectError("[Errno 61] Connection refused")), "WV-CONNECT-UNREACHABLE"),
        (raises(httpx.ConnectError("[Errno 8] nodename nor servname provided")), "WV-CONNECT-UNREACHABLE"),
        (tls_failure, "WV-CONNECT-TLS"),
        (raises(ssl.SSLError(1, "wrong version number")), "WV-CONNECT-TLS"),
        (raises(httpx.ConnectTimeout("connect timed out")), "WV-CONNECT-TIMEOUT"),
        (raises(httpx.ReadTimeout("read timed out")), "WV-CONNECT-TIMEOUT"),
        (raises(httpx.ReadError("connection reset")), "WV-CONNECT-UNREACHABLE"),
        (raises(httpx.RemoteProtocolError("garbage status line")), "WV-CONNECT-NOT-WEAVE"),
    ],
)
async def test_transport_failures_are_distinguished(handler, expected):
    transport, _ = serve(handler)
    with pytest.raises(ServerConfigError) as failure:
        await fetch_client_configuration("https://weave.example", transport=transport)
    assert code_of(failure) == expected and failure.value.exit_code == 3


async def test_total_time_is_bounded_even_when_bytes_trickle():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=DOCUMENT)

    transport, _ = serve(slow)
    with pytest.raises(ServerConfigError) as failure:
        await fetch_client_configuration("https://weave.example", transport=transport, timeout=0.05)
    assert code_of(failure) == "WV-CONNECT-TIMEOUT"


@pytest.mark.parametrize("control", ["\u001b", "\u007f", "\u0085", "\u009b"])
@pytest.mark.parametrize("where", ["document", "option"])
async def test_announced_labels_with_terminal_controls_are_not_accepted(control, where):
    label = f"Acme{control}[31mWeave"
    if where == "document":
        document = {**DOCUMENT, "display_name": label}
    else:
        document = {**DOCUMENT, "sign_in": [{**OPTION, "display_name": label}]}
    transport, _ = serve(lambda request: httpx.Response(200, json=document))
    with pytest.raises(ServerConfigError) as failure:
        await fetch_client_configuration("https://weave.example", transport=transport)
    assert code_of(failure) == "WV-CONNECT-NOT-WEAVE" and control not in str(failure.value.detail)


async def test_rejected_addresses_never_send_a_request():
    transport, calls = serve(lambda request: httpx.Response(200, json=DOCUMENT))
    for address in ("http://weave.example", "https://169.254.169.254", "https://weave.example/x"):
        with pytest.raises(ServerConfigError):
            await fetch_client_configuration(address, transport=transport)
    with pytest.raises(ValueError):
        await fetch_client_configuration("https://weave.example", transport=transport, timeout=0)
    assert calls == []


async def test_loopback_http_development_server():
    local = {**OPTION, "issuer": "http://localhost:18081/realms/weave", "allow_loopback_http": True}
    transport, calls = serve(lambda request: httpx.Response(200, json={**DOCUMENT, "sign_in": [local]}))
    configuration = await fetch_client_configuration("http://localhost:8000", transport=transport)
    assert str(calls[0].url) == "http://localhost:8000/api/v1/client-configuration"
    login = login_config_for(configuration.sign_in[0], server="http://localhost:8000", account="local")
    assert login.allow_loopback_http and login.target == "http://localhost:8000"
    assert issuer_origin(configuration.sign_in[0]) == "http://localhost:18081"


# --- login configuration -----------------------------------------------------------------------------------------


def test_login_config_comes_only_from_the_reviewed_option():
    option = SignInOption.model_validate(OPTION)
    login = login_config_for(option, server="Weave.Example", account="prod")
    assert login == LoginConfig(
        provider_id="acme",
        issuer="https://login.example/realms/acme",
        client_id="weave-cli",
        target="https://weave.example",
        account="prod",
        scopes=("openid",),
        trusted_endpoint_origins=("https://device.example",),
        allow_loopback_http=False,
        require_refresh_rotation=True,
    )
    assert issuer_origin(option) == "https://login.example"
    with pytest.raises(ServerConfigError) as failure:
        login_config_for(option, server="http://localhost:8000", account="prod")
    assert code_of(failure) == "WV-CONNECT-PROVIDER"
    with pytest.raises(ProfileError):
        login_config_for(option, server="https://weave.example", account="bad/name")
    with pytest.raises(ServerConfigError) as failure:
        login_config_for(option, server="http://weave.example", account="prod")
    assert code_of(failure) == "WV-CONNECT-INSECURE"


def test_only_a_loopback_server_may_propose_a_plain_http_provider():
    local = SignInOption.model_validate(
        {**OPTION, "issuer": "http://localhost:18081/realms/weave", "allow_loopback_http": True}
    )
    assert login_config_for(local, server="http://127.0.0.1:8000", account="local").allow_loopback_http
    with pytest.raises(ServerConfigError) as failure:
        login_config_for(local, server="https://weave.example", account="local")
    assert code_of(failure) == "WV-CONNECT-PROVIDER" and failure.value.detail == "WV-AUTH-TRUST"
    secure = SignInOption.model_validate({**OPTION, "allow_loopback_http": True})
    assert not login_config_for(secure, server="https://weave.example", account="prod").allow_loopback_http


# --- provider probe ----------------------------------------------------------------------------------------------


def login(**changes):
    return LoginConfig(
        provider_id="acme",
        issuer="https://login.example/realms/acme",
        client_id="weave-cli",
        target="https://weave.example",
        account="prod",
        scopes=("openid",),
        **changes,
    )


def metadata(**changes):
    base = "https://login.example/realms/acme/protocol/openid-connect"
    return {
        "issuer": "https://login.example/realms/acme",
        "authorization_endpoint": base + "/auth",
        "token_endpoint": base + "/token",
        "device_authorization_endpoint": base + "/auth/device",
        "revocation_endpoint": base + "/revoke",
        "code_challenge_methods_supported": ["plain", "S256"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        **changes,
    }


async def test_probe_reports_supported_flows_through_shared_discovery():
    transport, calls = serve(lambda request: httpx.Response(200, json=metadata()))
    assert await probe_sign_in(login(), transport=transport) == ProviderCheck(
        browser=True, device=True, revocation=True
    )
    assert str(calls[0].url) == "https://login.example/realms/acme/.well-known/openid-configuration"
    assert "authorization" not in calls[0].headers
    reduced = metadata(
        device_authorization_endpoint=None,
        revocation_endpoint=None,
        code_challenge_methods_supported=["plain"],
    )
    transport, _ = serve(lambda request: httpx.Response(200, json=reduced))
    assert await probe_sign_in(login(), transport=transport) == ProviderCheck(
        browser=False, device=False, revocation=False
    )
    no_device_grant = metadata(grant_types_supported=["authorization_code"])
    transport, _ = serve(lambda request: httpx.Response(200, json=no_device_grant))
    assert (await probe_sign_in(login(), transport=transport)).device is False
    unlisted = {key: value for key, value in metadata().items() if not key.endswith("_supported")}
    transport, _ = serve(lambda request: httpx.Response(200, json=unlisted))
    assert await probe_sign_in(login(), transport=transport) == ProviderCheck(
        browser=True, device=True, revocation=True
    )


async def test_probe_honors_reviewed_extra_origins():
    elsewhere = metadata(device_authorization_endpoint="https://device.example/device")
    transport, _ = serve(lambda request: httpx.Response(200, json=elsewhere))
    with pytest.raises(ServerConfigError) as failure:
        await probe_sign_in(login(), transport=transport)
    assert code_of(failure) == "WV-CONNECT-PROVIDER" and failure.value.detail == "WV-AUTH-TRUST"
    reviewed = login(trusted_endpoint_origins=("https://device.example",))
    assert (await probe_sign_in(reviewed, transport=transport)).device


@pytest.mark.parametrize(
    "handler, detail",
    [
        (lambda request: httpx.Response(200, json=metadata(issuer="https://evil.example")), "WV-AUTH-TRUST"),
        (
            lambda request: httpx.Response(200, json=metadata(token_endpoint="https://evil.example/token")),
            "WV-AUTH-TRUST",
        ),
        (lambda request: httpx.Response(500, text="secret-sentinel"), "WV-AUTH-PROVIDER"),
        (lambda request: httpx.Response(302, headers={"location": "https://evil.example"}), "WV-AUTH-PROVIDER"),
        (raises(httpx.ConnectError("refused")), "WV-AUTH-PROVIDER"),
        (lambda request: httpx.Response(200, json=metadata(authorization_endpoint=None)), "WV-AUTH-PROVIDER"),
    ],
)
async def test_probe_failures_map_to_provider_code(handler, detail):
    transport, _ = serve(handler)
    with pytest.raises(ServerConfigError) as failure:
        await probe_sign_in(login(), transport=transport)
    assert code_of(failure) == "WV-CONNECT-PROVIDER" and failure.value.detail == detail
    assert "secret-sentinel" not in str(failure.value)
