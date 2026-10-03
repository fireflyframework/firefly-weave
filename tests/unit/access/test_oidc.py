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

"""Signed hermetic tokens, never represented as live provider tokens."""

import asyncio
import importlib.util
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def oidc():
    assert importlib.util.find_spec("firefly_weave.access.oidc") is not None, "OIDC boundary absent"
    from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "a", "alg": "RS256"}
    state = {"clock": 0, "fetches": 0, "fail": False, "keys": [jwk]}

    async def fetch():
        state["fetches"] += 1
        await asyncio.sleep(0)
        if state["fail"]:
            raise OSError("secret backend detail")
        return {"keys": state["keys"]}

    config = ProviderConfig(
        provider_id="kc",
        issuer="https://trusted.test/realm",
        jwks_uri="https://trusted.test/jwks",
        audience="weave-api",
        clients={"host": "application"},
        cache_seconds=30,
        refresh_seconds=5,
    )
    verifier = OIDCVerifier(config, fetch=fetch, clock=lambda: state["clock"])

    def token(**changes):
        claims = {
            "iss": config.issuer,
            "aud": config.audience,
            "sub": "subject",
            "exp": int(time.time()) + 300,
            "typ": "Bearer",
            "azp": "host",
        } | changes
        return jwt.encode(
            {k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256", headers={"kid": "a"}
        )

    return verifier, token, state, key


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "evil"},
        {"aud": "other"},
        {"sub": None},
        {"sub": ""},
        {"exp": None},
        {"exp": 1},
        {"typ": "ID"},
        {"typ": None},
        {"azp": "evil"},
        {"nbf": int(time.time()) + 10000},
    ],
)
async def test_strict_access_policy(oidc, change):
    from firefly_weave.access.oidc import AuthenticationFailed

    verifier, token, _, _ = oidc
    assert (await verifier.verify(token())).actor_kind == "application"
    with pytest.raises(AuthenticationFailed, match="Authentication failed"):
        await verifier.verify(token(**change))


async def test_cache_rotation_outage_and_unknown_key_storm(oidc):
    from firefly_weave.access.oidc import AuthenticationFailed

    verifier, token, state, key = oidc
    await verifier.verify(token())
    state["clock"] = 6
    state["fail"] = True

    async def unknown(i):
        value = jwt.encode({"exp": int(time.time()) + 60}, key, algorithm="RS256", headers={"kid": str(i)})
        with pytest.raises(AuthenticationFailed):
            await verifier.verify(value)

    await asyncio.gather(*(unknown(i) for i in range(100)))
    assert state["fetches"] == 2
    await verifier.verify(token())
    state["clock"] = 31
    with pytest.raises(AuthenticationFailed):
        await verifier.verify(token())
    state["clock"] = 37
    state["fail"] = False
    state["keys"][0]["kid"] = "b"
    rotated = jwt.encode(
        jwt.decode(token(), options={"verify_signature": False}), key, algorithm="RS256", headers={"kid": "b"}
    )
    await verifier.verify(rotated)
    with pytest.raises(AuthenticationFailed):
        await verifier.verify(token())


async def test_unsigned_and_hmac_confusion(oidc):
    from firefly_weave.access.oidc import AuthenticationFailed

    verifier, token, _, _ = oidc
    claims = jwt.decode(token(), options={"verify_signature": False})
    for algorithm, key in [("none", ""), ("HS256", "a" * 32)]:
        with pytest.raises(AuthenticationFailed):
            await verifier.verify(jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "a"}))


def test_endpoints_are_trusted_and_tls_required():
    assert importlib.util.find_spec("firefly_weave.access.oidc") is not None, "OIDC boundary absent"
    from firefly_weave.access.oidc import ProviderConfig

    for url in ["http://evil.test/keys", "ftp://trusted.test/keys", "https://user:pass@trusted.test/keys"]:
        with pytest.raises(ValueError):
            ProviderConfig(
                provider_id="p", issuer="https://trusted.test", jwks_uri=url, audience="a", clients={"c": "human"}
            )


@pytest.mark.parametrize("provider", ["keycloak", "entra", "generic"])
def test_provider_claim_profiles_cannot_create_local_grants(provider):
    from uuid import uuid4

    from firefly_weave.access.authorization import AccessDenied, AuthorizationService
    from firefly_weave.access.models import Grant, Principal, VerifiedIdentity
    from firefly_weave.access.providers.entra import EntraClaimsMapper
    from firefly_weave.access.providers.generic import GenericClaimsMapper
    from firefly_weave.access.providers.keycloak import KeycloakClaimsMapper
    from firefly_weave.contracts.access import Scope

    mappers = {"keycloak": KeycloakClaimsMapper(), "entra": EntraClaimsMapper(), "generic": GenericClaimsMapper()}
    claims = {
        "roles": ["developer"],
        "resource_access": {"weave-api": {"roles": ["developer"]}, "evil": {"roles": ["platform_admin"]}},
        "realm_access": {"roles": ["platform_admin"]},
        "scope": "compile",
        "scp": "compile",
    }
    identity = VerifiedIdentity(
        provider_id=provider,
        issuer="https://trusted.test",
        subject="same",
        client_id="host",
        actor_kind="application",
        claims=claims,
    )
    mapped = mappers[provider].map(identity)
    assert mapped.application_roles == ("developer",) and not mapped.delegated_scopes
    principal = Principal(id=uuid4(), kind="application")
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    with pytest.raises(AccessDenied):
        AuthorizationService().require(principal, scope, "definition.write")
    bound = principal.model_copy(update={"grants": (Grant(role="developer", scope=scope),)})
    AuthorizationService().require(bound, scope, "definition.write")


async def test_async_fetch_deadline_cancels_inflight_work(oidc):
    from firefly_weave.access.oidc import AuthenticationFailed, OIDCVerifier

    original, token, _, _ = oidc
    closed = asyncio.Event()

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    verifier = OIDCVerifier(original.config.model_copy(update={"timeout_seconds": 0.01}), fetch=stalled)
    with pytest.raises(AuthenticationFailed):
        await asyncio.wait_for(verifier.verify(token()), timeout=1)
    assert closed.is_set()


async def test_async_fetch_caller_cancellation_propagates(oidc):
    from firefly_weave.access.oidc import OIDCVerifier

    original, token, _, _ = oidc
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stalled():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    verifier = OIDCVerifier(original.config, fetch=stalled)
    task = asyncio.create_task(verifier.verify(token()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_signed_custom_client_and_payload_profile_preserves_actor_policy(oidc):
    from firefly_weave.access.oidc import AuthenticationFailed, OIDCVerifier

    original, token, state, _ = oidc

    async def fetch():
        return {"keys": state["keys"]}

    config = original.config.model_copy(
        update={
            "client_claim": "appid",
            "token_class_claim": "token_use",
            "token_class_value": "access",
            "clients": {"delegated-client": "human"},
        }
    )
    verifier = OIDCVerifier(config, fetch=fetch)
    identity = await verifier.verify(token(appid="delegated-client", token_use="access", typ=None, azp=None))
    assert identity.actor_kind == "human" and identity.client_id == "delegated-client"
    for changes in ({"token_use": "id"}, {"appid": "host"}):
        values = {"appid": "delegated-client", "token_use": "access"} | changes
        with pytest.raises(AuthenticationFailed):
            await verifier.verify(token(**values))


async def test_entra_style_keys_without_alg_verify_by_key_family(oidc):
    from firefly_weave.access.oidc import AuthenticationFailed

    verifier, token, state, key = oidc
    published = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    # Microsoft Entra ID publishes RSA signing keys with kid, x5t and x5c but no alg.
    state["keys"] = [published | {"kid": "a", "use": "sig", "x5t": "thumbprint", "x5c": ["certificate"]}]
    assert (await verifier.verify(token())).subject == "subject"
    # A token that claims another algorithm for the same key is still refused.
    claims = jwt.decode(token(), options={"verify_signature": False})
    with pytest.raises(AuthenticationFailed):
        await verifier.verify(jwt.encode(claims, "a" * 32, algorithm="HS256", headers={"kid": "a"}))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"kty": "RSA"}, "RS256"),
        ({"kty": "RSA", "use": "sig"}, "RS256"),
        ({"kty": "RSA", "use": "enc"}, None),
        ({"kty": "oct"}, None),
        ({"kty": "EC", "crv": "P-256"}, None),
        ({"kty": "RSA", "alg": "HS256"}, None),
        ({"kty": "RSA", "alg": "RS512"}, None),
        ({"kty": "RSA", "alg": "RS256"}, "RS256"),
    ],
)
def test_only_allowed_signing_families_are_accepted(raw, expected):
    from firefly_weave.access.oidc import key_algorithm

    assert key_algorithm(raw, ("RS256",)) == expected


def test_elliptic_keys_need_the_allowed_curve():
    from firefly_weave.access.oidc import key_algorithm

    assert key_algorithm({"kty": "EC", "crv": "P-256"}, ("RS256", "ES256")) == "ES256"
    assert key_algorithm({"kty": "EC", "crv": "P-384"}, ("RS256", "ES256")) is None
