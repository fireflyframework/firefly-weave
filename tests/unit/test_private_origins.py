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

"""C8 private-origin policy: file format, address rules, decisions, legacy settings and audit."""

import errno
import json
import logging
import os
import socket
import types

import pytest

from firefly_weave import private_origins as po

ACME = "http://acme.acceptance.test:8080"
EGRESS = "10.231.1.0/24"
FIXTURE = "10.231.1.37"


def not_local(_address):
    return False


def local(_address):
    return True


def entry(origin=ACME, purpose="http-connector", networks=(EGRESS,), credentials="bridge"):
    return po.PrivateOrigin(origin=origin, purpose=purpose, networks=networks, credentials=credentials)


def policy(*entries):
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(entries)


def document(*entries, **top):
    value = {"format": po.FORMAT, "platform": po.PLATFORM, "entries": list(entries), **top}
    return json.dumps(value).encode()


FILE_ENTRY = {"origin": ACME, "purpose": "http-connector", "networks": [EGRESS], "credentials": "bridge"}


def test_the_purposes_table_names_the_ten_c8_purposes():
    assert set(po.PURPOSES) == {
        "model",
        "worker-auth",
        "platform-api",
        "runner",
        "http-connector",
        "event-delivery",
        "mail",
        "broker",
        "file-transfer",
        "database",
    }
    assert po.PURPOSES["http-connector"].plaintext == {"http"} and po.PURPOSES["http-connector"].connector
    assert not po.PURPOSES["runner"].connector
    # Only connectors and webhooks keep plain text to public addresses (owner decision, C8).
    assert {name for name, rule in po.PURPOSES.items() if rule.public_plaintext} == {"http-connector", "event-delivery"}


@pytest.mark.parametrize(
    ("value", "purpose", "expected"),
    [
        ("HTTP://Acme.Acceptance.Test:8080/", "http-connector", ACME),
        ("http://acme.acceptance.test", "http-connector", "http://acme.acceptance.test:80"),
        ("https://[FD00::1]:8443", "model", "https://[fd00::1]:8443"),
        ("smtp://mail.acceptance.test", "mail", "smtp://mail.acceptance.test:25"),
        ("http://10.231.1.5:8080", "event-delivery", "http://10.231.1.5:8080"),
    ],
)
def test_canonical_origins(value, purpose, expected):
    assert po.canonical_origin(value, purpose) == expected


@pytest.mark.parametrize(
    ("value", "purpose"),
    [
        ("ftp://acme.acceptance.test", "http-connector"),
        ("smtp://acme.acceptance.test", "http-connector"),
        ("http://user@acme.acceptance.test", "http-connector"),
        ("http://acme.acceptance.test/orders", "http-connector"),
        ("http://acme.acceptance.test?x=1", "http-connector"),
        ("http://acme.acceptance.test#top", "http-connector"),
        ("http://acme.acceptance.test:0", "http-connector"),
        ("http://acme.acceptance.test:99999", "http-connector"),
        ("http://acme.acceptance.test.", "http-connector"),
        ("http://acme%2eacceptance.test", "http-connector"),
        ("http://acme acceptance.test", "http-connector"),
        ("http://acme_acceptance.test", "http-connector"),
        (ACME, "not-a-purpose"),
    ],
)
def test_origins_outside_the_exact_form_are_refused(value, purpose):
    with pytest.raises(ValueError):
        po.canonical_origin(value, purpose)


@pytest.mark.parametrize(
    "value",
    [
        "169.254.169.254",
        "fd00:ec2::254",
        "100.100.100.200",
        "168.63.129.16",
        "fe80::1",
        "224.0.0.1",
        "0.0.0.0",
        "::",
        "240.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::1",
        "2002:a9fe:a9fe::1",
        "2001:0:4136:e378:8000:63bf:5601:5601",
    ],
)
def test_always_refused_addresses_are_decoded_first(value):
    assert po.always_denied(value)


# CGNAT is refused by default like any non-global range, but an entry can list it (C8).
@pytest.mark.parametrize(
    "value",
    [
        "10.0.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "::1",
        "fd12::1",
        "8.8.8.8",
        "2606:4700::1111",
        "::ffff:10.0.0.1",
        "100.64.0.1",
    ],
)
def test_ordinary_addresses_are_not_always_refused(value):
    assert not po.always_denied(value)


def test_entry_ranges_are_loopback_rfc1918_cgnat_and_ula():
    for value in (
        "127.0.0.1",
        "10.1.2.3",
        "172.20.0.1",
        "192.168.0.1",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "::ffff:10.0.0.1",
    ):
        assert po.is_private(value), value
    for value in ("8.8.8.8", "192.0.2.1", "2606:4700::1111"):
        assert not po.is_private(value), value


def test_local_addresses_are_the_ones_this_process_can_bind():
    assert po.is_local_address("127.0.0.1")
    assert not po.is_local_address("192.0.2.1")


def failing_socket_module(error, *, on_create=False):
    """A stand-in for the socket module whose sockets raise ``error`` when created or bound."""

    class Probe:
        def __init__(self, family, kind):
            if on_create:
                raise OSError(error, os.strerror(error))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def bind(self, address):
            raise OSError(error, os.strerror(error))

    return types.SimpleNamespace(
        AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6, SOCK_STREAM=socket.SOCK_STREAM, socket=Probe
    )


@pytest.mark.parametrize("on_create", [False, True], ids=["bind", "create"])
@pytest.mark.parametrize("error", [errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT], ids=errno.errorcode.get)
def test_only_an_address_or_family_this_host_lacks_is_not_local(monkeypatch, error, on_create):
    monkeypatch.setattr(po, "socket", failing_socket_module(error, on_create=on_create))
    assert not po.is_local_address(FIXTURE)
    assert not po.is_local_address("fd00::2")
    assert policy(entry()).check("http-connector", ACME + "/x", [FIXTURE]).origin == ACME


@pytest.mark.parametrize("on_create", [False, True], ids=["bind", "create"])
@pytest.mark.parametrize(
    "error",
    [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.EADDRINUSE, errno.EACCES, errno.EPERM, errno.EINVAL],
    ids=errno.errorcode.get,
)
def test_any_other_probe_error_counts_as_local_so_the_connection_is_refused(monkeypatch, error, on_create):
    monkeypatch.setattr(po, "socket", failing_socket_module(error, on_create=on_create))
    assert po.is_local_address(FIXTURE)
    assert po.is_local_address("fd00::2")
    with pytest.raises(po.PrivateOriginDenied) as refused:
        policy(entry()).check("http-connector", ACME + "/x", [FIXTURE])
    assert refused.value.reason == "control-plane"


def test_parse_reads_a_valid_document():
    data = document(FILE_ENTRY, {**FILE_ENTRY, "purpose": "event-delivery"})
    parsed = po.parse(data)
    assert [(e.origin, e.purpose, e.label) for e in parsed.entries] == [
        (ACME, "http-connector", "Development only"),
        (ACME, "event-delivery", "Development only"),
    ]
    assert parsed.platform == po.PLATFORM and len(parsed.file_sha256) == 64


@pytest.mark.parametrize(
    "data",
    [
        b'{"format": "weave/private-origins-v1", "format": "x", "platform": "local-development", "entries": []}',
        document(FILE_ENTRY, extra=True),
        document({**FILE_ENTRY, "label": "Development only"}),
        json.dumps({"format": "weave/private-origins-v2", "platform": po.PLATFORM, "entries": []}).encode(),
        json.dumps({"format": po.FORMAT, "platform": "production", "entries": []}).encode(),
        document({**FILE_ENTRY, "origin": "HTTP://acme.acceptance.test:8080"}),
        document({**FILE_ENTRY, "networks": ["8.8.8.0/24"]}),
        document({**FILE_ENTRY, "networks": ["172.16.0.0/12"]}),
        document({**FILE_ENTRY, "networks": ["100.64.0.0/10"]}),
        document({**FILE_ENTRY, "networks": ["10.231.1.1/24"]}),
        document({**FILE_ENTRY, "networks": []}),
        document({**FILE_ENTRY, "credentials": "always"}),
        document({**FILE_ENTRY, "origin": "smtp://acme.acceptance.test:25"}),
        document(FILE_ENTRY, FILE_ENTRY),
        b"not json",
        b" " * (po.MAX_FILE_BYTES + 1),
    ],
)
def test_parse_refuses_documents_outside_the_format(data):
    with pytest.raises(po.PrivateOriginsInvalid):
        po.parse(data)


def test_render_is_canonical_and_round_trips():
    original = policy(entry(purpose="http-connector"), entry(purpose="event-delivery"))
    data = po.render(original)
    assert json.loads(data)["entries"][0]["purpose"] == "event-delivery"
    assert po.render(po.parse(data)) == data
    assert [(e.origin, e.purpose) for e in po.parse(data).entries] == [
        (ACME, "event-delivery"),
        (ACME, "http-connector"),
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and symbolic links")
def test_read_file_refuses_loose_or_linked_files(tmp_path):
    good = tmp_path / "private-origins.json"
    good.write_bytes(document(FILE_ENTRY))
    good.chmod(0o444)
    assert po.read_file(good).entries[0].origin == ACME
    loose = tmp_path / "loose.json"
    loose.write_bytes(document(FILE_ENTRY))
    loose.chmod(0o666)
    link = tmp_path / "link.json"
    link.symlink_to(good)
    for path in (loose, link, tmp_path, tmp_path / "missing.json"):
        with pytest.raises(po.PrivateOriginsInvalid):
            po.read_file(path)


def test_plain_http_reaches_an_exact_entry_inside_its_network():
    approved = policy(entry())
    found = approved.check("http-connector", ACME + "/orders/O-1/status", [FIXTURE], local=not_local)
    assert found is not None and found.origin == ACME


@pytest.mark.parametrize(
    ("url", "addresses", "reason"),
    [
        (ACME + "/x", ["8.8.8.8"], "outside-networks"),
        (ACME + "/x", ["10.231.2.5"], "outside-networks"),
        ("http://other.acceptance.test:8080/x", [FIXTURE], "no-entry"),
        (ACME + "/x", ["169.254.169.254"], "always-refused"),
        ("http://metadata.google.internal/x", ["10.231.1.9"], "metadata"),
        ("http://weave-api.weave-system.svc:8000/x", ["10.231.1.9"], "cluster-service"),
        ("ftp://acme.acceptance.test/x", [FIXTURE], "scheme"),
        (ACME + "/x", [], "unresolved"),
        (ACME + "/x", ["not-an-address"], "address"),
    ],
)
def test_refusals_name_their_reason(url, addresses, reason):
    with pytest.raises(po.PrivateOriginDenied) as refused:
        policy(entry()).check("http-connector", url, addresses, local=not_local)
    assert refused.value.reason == reason


def test_mixed_answers_are_refused():
    # An exact entry pins its origin to its networks, so a public address in the answer is refused.
    with pytest.raises(po.PrivateOriginDenied) as refused:
        policy(entry()).check("http-connector", ACME + "/x", [FIXTURE, "8.8.8.8"], local=not_local)
    assert refused.value.reason == "outside-networks"


def test_https_to_public_addresses_needs_no_entry_but_private_https_does():
    empty = po.PrivateOrigins.empty()
    assert empty.check("http-connector", "https://api.example.com/x", ["8.8.8.8"], local=not_local) is None
    with pytest.raises(po.PrivateOriginDenied):
        empty.check("http-connector", "https://api.internal.test/x", ["10.1.2.3"], local=not_local)


@pytest.mark.parametrize("purpose", ["http-connector", "event-delivery"])
@pytest.mark.parametrize("sends_credentials", [False, True])
def test_public_plain_http_keeps_working_for_connectors_and_webhooks(purpose, sends_credentials):
    # Owner decision (C8): no entry is needed, with or without credentials, exactly as before C8.
    for approved in (po.PrivateOrigins.empty(), policy(entry(), entry(purpose="event-delivery"))):
        found = approved.check(
            purpose,
            "http://api.example.com/orders",
            ["93.184.216.34", "2606:4700::1111"],
            sends_credentials=sends_credentials,
            local=not_local,
        )
        assert found is None


def test_public_plain_http_is_refused_for_model_endpoints():
    models = "http://models.example.com:80"
    listed = policy(entry(origin=models, purpose="model", credentials="none"))
    for approved in (po.PrivateOrigins.empty(), listed):
        with pytest.raises(po.PrivateOriginDenied) as refused:
            approved.check("model", models + "/v1/models", ["93.184.216.34"], local=not_local)
        assert refused.value.reason == "public-plain-text"
    https = po.PrivateOrigins.empty().check(
        "model", "https://models.example.com/v1", ["93.184.216.34"], local=not_local
    )
    assert https is None


TAILNET = "http://tailnet-api.acceptance.test:8080"


def test_cgnat_needs_an_explicit_network():
    with pytest.raises(po.PrivateOriginDenied) as refused:
        po.PrivateOrigins.empty().check("http-connector", TAILNET + "/x", ["100.100.100.7"], local=not_local)
    assert refused.value.reason == "no-entry"
    listed = policy(entry(origin=TAILNET, networks=("100.100.100.0/24",)))
    assert listed.check("http-connector", TAILNET + "/x", ["100.100.100.7"], local=not_local) is not None
    with pytest.raises(po.PrivateOriginDenied) as refused:
        listed.check("http-connector", TAILNET + "/x", ["100.100.101.7"], local=not_local)
    assert refused.value.reason == "outside-networks"
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("http-connector", "event-delivery"), ("100.64.0.0/10",), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    )
    assert legacy.check("event-delivery", TAILNET + "/x", ["100.100.100.7"], local=not_local) is not None


def test_the_cgnat_metadata_address_is_refused_even_inside_an_entry():
    listed = policy(entry(origin=TAILNET, networks=("100.100.100.0/24",)))
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("http-connector",), ("100.64.0.0/10",), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    )
    for approved in (listed, legacy):
        with pytest.raises(po.PrivateOriginDenied) as refused:
            approved.check("http-connector", TAILNET + "/x", ["100.100.100.200"], local=not_local)
        assert refused.value.reason == "always-refused"


def test_connector_clients_never_reach_this_process_but_runners_may():
    with pytest.raises(po.PrivateOriginDenied) as refused:
        policy(entry()).check("http-connector", ACME + "/x", [FIXTURE], local=local)
    assert refused.value.reason == "control-plane"
    runner = policy(entry(origin="http://weave-api.weave-system.svc:8000", purpose="runner"))
    assert runner.check("runner", "http://weave-api.weave-system.svc:8000/api", [FIXTURE], local=local)


def test_credentials_over_plain_text_follow_the_entry():
    def sends(approved, url=ACME, addresses=(FIXTURE,), purpose="http-connector"):
        return approved.check(purpose, url, list(addresses), sends_credentials=True, local=not_local)

    assert sends(policy(entry()))
    with pytest.raises(po.PrivateOriginDenied) as refused:
        sends(policy(entry(credentials="none")))
    assert refused.value.reason == "credentials"
    gateway = "http://gateway.acceptance.test:9000"
    loopback = entry(origin=gateway, purpose="model", networks=("127.0.0.0/8",), credentials="loopback")
    assert sends(policy(loopback), gateway, ("127.0.0.1",), "model")
    with pytest.raises(po.PrivateOriginDenied):
        sends(policy(entry(credentials="loopback")))


def test_legacy_settings_keep_todays_reach():
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("http-connector", "event-delivery"), ("127.0.0.0/8",), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    )
    url = "http://127.0.0.1:9000/x"
    found = legacy.check("http-connector", url, ["127.0.0.1"], sends_credentials=True, local=local)
    assert found is not None and found.label == "Legacy setting" and found.origin is None
    # Public addresses keep plain HTTP, and each address of an answer is checked on its own, as today.
    assert legacy.check("http-connector", "http://public.example.test/x", ["93.184.216.34"], local=not_local) is None
    mixed = legacy.check("event-delivery", "http://mixed.example.test/x", ["127.0.0.1", "93.184.216.34"], local=local)
    assert mixed is not None and mixed.source == "legacy"
    with pytest.raises(po.PrivateOriginDenied) as refused:
        legacy.check("http-connector", "http://internal.example.test/x", ["10.1.2.3"], local=not_local)
    assert refused.value.reason == "outside-networks"
    assert not legacy.permits_plaintext("http-connector", "http://127.0.0.1:9000")
    again = legacy.with_legacy(("http-connector",), ("10.0.0.0/8",), setting="WEAVE_HTTP_PRIVATE_NETWORKS")
    (merged,) = again.for_purpose("http-connector")
    assert merged.networks == ("127.0.0.0/8", "10.0.0.0/8")


def test_legacy_settings_keep_refusing_ipv6_loopback():
    # Today's check refuses every reserved address, IPv6 loopback among them, whatever the setting lists.
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("http-connector", "event-delivery"), ("127.0.0.0/8", "::1/128"), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    )
    url = "http://localhost:9000/x"
    for addresses in (["::1"], ["127.0.0.1", "::1"]):
        with pytest.raises(po.PrivateOriginDenied) as refused:
            legacy.check("event-delivery", url, addresses, local=not_local)
        assert refused.value.reason == "always-refused"
    for addresses in (["127.0.0.1"], ["::ffff:127.0.0.1"]):
        assert legacy.check("http-connector", url, addresses, local=not_local) is not None
    # A development entry keeps its own rule: IPv6 loopback inside its networks stays reachable.
    gateway = "http://gateway.acceptance.test:9000"
    listed = policy(entry(origin=gateway, purpose="model", networks=("::1/128",), credentials="loopback"))
    assert listed.check("model", gateway + "/v1", ["::1"], local=not_local) is not None


def test_legacy_plain_text_networks_limit_plain_text_only():
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("database",), ("10.0.0.0/8",), setting="WEAVE_POSTGRES_PRIVATE_NETWORKS", plaintext_networks=("10.9.0.0/16",)
    )
    url = "postgresql://db.internal.test:5432"
    assert legacy.check("database", url, ["10.1.0.5"], plaintext=False, local=not_local)
    assert legacy.check("database", url, ["10.9.0.5"], plaintext=True, local=not_local)
    with pytest.raises(po.PrivateOriginDenied):
        legacy.check("database", url, ["10.1.0.5"], plaintext=True, local=not_local)


def test_the_postgres_settings_keep_todays_reach_and_plain_text_rule():
    # As PostgresConnector does today: reach comes from the private networks only, and the
    # plain-text networks are an extra condition for plain text, never a second source of reach.
    url = "postgresql://db.internal.test:5432"

    def database(private, plaintext):
        return po.load(
            {
                "WEAVE_POSTGRES_PRIVATE_NETWORKS": json.dumps(private),
                "WEAVE_POSTGRES_PLAINTEXT_NETWORKS": json.dumps(plaintext),
            }
        )

    only_plain = database([], ["10.0.0.0/8"])
    for plain in (False, True):
        with pytest.raises(po.PrivateOriginDenied) as refused:
            only_plain.check("database", url, ["10.1.2.3"], plaintext=plain, local=not_local)
        assert refused.value.reason == "outside-networks"
    both = database(["10.0.0.0/8"], ["8.8.8.0/24", "10.9.0.0/16"])
    (mapped,) = both.for_purpose("database")
    assert mapped.networks == ("10.0.0.0/8",) and mapped.plaintext_networks == ("8.8.8.0/24", "10.9.0.0/16")
    assert both.check("database", url, ["10.9.0.5"], plaintext=True, local=not_local)
    assert both.check("database", url, ["8.8.8.8"], plaintext=True, local=not_local)
    assert both.check("database", url, ["8.8.4.4"], plaintext=False, local=not_local) is None
    with pytest.raises(po.PrivateOriginDenied):
        both.check("database", url, ["8.8.4.4"], plaintext=True, local=not_local)
    public_plain = database([], ["8.8.8.0/24"])
    assert public_plain.check("database", url, ["8.8.8.8"], plaintext=True, local=not_local)


def test_legacy_entries_that_cannot_be_built_are_invalid():
    too_many = [f"10.{index}.0.0/16" for index in range(129)]
    with pytest.raises(po.PrivateOriginsInvalid):
        po.PrivateOrigins.empty().with_legacy(("database",), too_many, setting="WEAVE_POSTGRES_PRIVATE_NETWORKS")
    with pytest.raises(po.PrivateOriginsInvalid):
        po.PrivateOrigins.empty().with_legacy(("mail",), ("10.0.0.1/8",), setting="WEAVE_MAIL_PRIVATE_NETWORKS")
    # A development entry still names at least one network.
    with pytest.raises(ValueError):
        entry(networks=())


def test_permits_plaintext_only_for_an_exact_plain_text_entry():
    approved = policy(entry(), entry(origin="https://secure.acceptance.test:8443"))
    assert approved.permits_plaintext("http-connector", ACME + "/")
    assert not approved.permits_plaintext("event-delivery", ACME)
    assert not approved.permits_plaintext("http-connector", "https://secure.acceptance.test:8443")


def test_load_reads_the_file_and_maps_legacy_settings(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="weave.private_origins")
    path = tmp_path / "private-origins.json"
    path.write_bytes(document(FILE_ENTRY))
    path.chmod(0o444)
    loaded = po.load({"WEAVE_PRIVATE_ORIGINS_FILE": str(path), "WEAVE_HTTP_PRIVATE_NETWORKS": '["10.0.0.0/8"]'})
    assert [(e.purpose, e.source) for e in loaded.entries] == [
        ("http-connector", "file"),
        ("http-connector", "legacy"),
        ("event-delivery", "legacy"),
    ]
    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert {
        "version": 1,
        "action": "private_origins.legacy",
        "setting": "WEAVE_HTTP_PRIVATE_NETWORKS",
        "purposes": ["http-connector", "event-delivery"],
    } in records
    assert records[-1]["action"] == "private_origins.loaded"
    assert po.load({}) == po.PrivateOrigins.empty()


@pytest.mark.parametrize(
    "environ",
    [
        {"WEAVE_PRIVATE_ORIGINS_FILE": "relative/private-origins.json"},
        {"WEAVE_HTTP_PRIVATE_NETWORKS": "not json"},
        {"WEAVE_HTTP_PRIVATE_NETWORKS": '["10.0.0.1/8"]'},
    ],
)
def test_load_refuses_unusable_configuration(environ):
    with pytest.raises(po.PrivateOriginsInvalid):
        po.load(environ)


def test_refusals_are_audited_without_paths_or_values(caplog):
    caplog.set_level(logging.WARNING, logger="weave.private_origins")
    with pytest.raises(po.PrivateOriginDenied):
        policy(entry()).check("http-connector", ACME + "/orders/O-1?token=do-not-log", ["8.8.8.8"], local=not_local)
    assert json.loads(caplog.records[-1].getMessage()) == {
        "version": 1,
        "action": "private_origins.refused",
        "purpose": "http-connector",
        "origin": ACME,
        "reason": "outside-networks",
    }
    assert "do-not-log" not in caplog.text and "/orders" not in caplog.text


def test_the_process_policy_can_be_installed_and_restored():
    approved = policy(entry())
    before = po.active()
    with po.installed(approved):
        assert po.active() is approved
    assert po.active() is before


def test_file_entries_need_the_local_development_platform():
    with pytest.raises(ValueError):
        po.PrivateOrigins(entries=(entry(),))
