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

"""J9.1's verdict on each answer: exactly 403 WV-FORBIDDEN when denied, the operation's own answer when permitted."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "acceptance_j9_security", ROOT / "tests/acceptance/test_j9_security.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


security = load()


def outcome(permitted, status, code, *, random=True):
    return {"permitted": permitted, "random": random, "status": status, "code": code}


@pytest.mark.parametrize(
    ("status", "code"),
    [(200, "ok"), (201, "ok"), (204, "ok"), (404, "WV-NOT-FOUND"), (409, "WV-REVISION"), (422, "WV-SCOPE")],
)
def test_a_permitted_call_answered_by_the_operation_is_right(status, code):
    assert not security.wrong(outcome(True, status, code))


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "WV-UNAUTHENTICATED"),
        (403, "WV-FORBIDDEN"),
        (403, "WV-SCOPE"),
        (400, "WV-BAD-REQUEST"),
        (429, "WV-RATE-LIMITED"),
        (500, "WV-INTERNAL"),
        (503, "WV-UNAVAILABLE"),
        (404, "not-a-weave-code"),
    ],
)
def test_a_permitted_call_refused_before_the_operation_answers_is_wrong(status, code):
    assert security.wrong(outcome(True, status, code))


def test_a_permitted_call_that_names_nothing_random_must_succeed_or_meet_an_earlier_call():
    assert not security.wrong(outcome(True, 200, "ok", random=False))
    assert not security.wrong(outcome(True, 409, "WV-VERSION-EXISTS", random=False))
    assert security.wrong(outcome(True, 404, "WV-NOT-FOUND", random=False))
    assert security.wrong(outcome(True, 422, "WV-SCOPE", random=False))


@pytest.mark.parametrize(
    ("status", "code", "is_wrong"),
    [(403, "WV-FORBIDDEN", False), (403, "WV-SCOPE", True), (404, "WV-NOT-FOUND", True), (200, "ok", True)],
)
def test_a_denied_call_is_exactly_403_wv_forbidden(status, code, is_wrong):
    assert security.wrong(outcome(False, status, code)) is is_wrong


def test_rows_that_name_a_random_id_are_told_apart():
    helper = importlib.util.spec_from_file_location(
        "acceptance_authorization", ROOT / "tests/acceptance/acceptance_authorization.py"
    )
    module = importlib.util.module_from_spec(helper)
    sys.modules[helper.name] = module
    helper.loader.exec_module(module)
    _, rows = module.load()
    by_name = {row.operation: security.names_random(row) for row in rows}
    assert by_name["incidents.read"] and by_name["runs.start"] and by_name["activations.create"]
    assert not by_name["definitions.publish"] and not by_name["language.read"]


def test_refusal_records_are_read_from_the_api_log_whatever_the_line_prefix():
    log = "\n".join(
        [
            'WARNING weave.private_origins {"action":"private_origins.refused","origin":"http://10.0.0.4:8080",'
            '"purpose":"http-connector","reason":"no-entry","version":1}',
            '2026-10-19T12:00:00Z {"action":"private_origins.refused","origin":"http://169.254.169.254:80",'
            '"purpose":"http-connector","reason":"always-refused","version":1}',
            '{"action":"private_origins.refused","origin":"http://10.0.0.4:8080","purpose":"mail","reason":"x"}',
            '{"action":"private_origins.refused", broken',
            "INFO request finished",
        ]
    )
    records = security.refusal_records(log)
    assert len(records) == 3
    origins = {"acme": "http://10.0.0.4:8080", "metadata": "http://169.254.169.254:80", "silent": "http://10.0.0.9:80"}
    # Only the connector purpose counts; a destination nobody refused has no reason, so it never passes as refused.
    assert security.refusal_reasons(records, origins) == {
        "acme": ["no-entry"],
        "metadata": ["always-refused"],
        "silent": [],
    }


def test_the_policy_refuses_the_service_by_address_and_the_metadata_address_by_their_rules():
    from firefly_weave.private_origins import PrivateOrigin, PrivateOriginDenied, PrivateOrigins

    policy = PrivateOrigins(
        platform="local-development",
        entries=(
            PrivateOrigin(
                origin="http://acme.acceptance.test:8080",
                purpose="http-connector",
                networks=("10.246.49.0/24",),
                credentials="bridge",
            ),
        ),
    )
    for name, url, address in (
        ("acme-address-j9", "http://10.246.49.4:8080", "10.246.49.4"),
        ("metadata-j9", "http://169.254.169.254", "169.254.169.254"),
    ):
        with pytest.raises(PrivateOriginDenied) as refused:
            policy.check("http-connector", url, [address], local=lambda ip: False)
        assert refused.value.reason == security.REFUSALS[name]
