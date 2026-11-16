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

"""Alert and approval mail without a database: the exact text, the budgets, retries and never sending twice."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.operations.alert_notifications import (
    ALERT_SIGNAL_NAMES,
    ENVIRONMENT_HOUR,
    RECIPIENT_HOUR,
    RULE_HOUR,
    Usage,
    admit,
    alert_email,
    approval_email,
    due,
    outcome,
    request_id,
)
from firefly_weave.operations.alert_signals import ALERT_SIGNALS

STARTED = datetime(2026, 11, 16, 14, 2, 31, tzinfo=UTC)
FOOTER = (
    "You get this because an alert rule in local sends to this address. Ask an administrator of local to change the "
    "rule's channels."
)


def alert(**changes):
    values = {
        "rule_name": "Run failure rate above 10 %",
        "severity": "critical",
        "signal": "run.failure_rate",
        "comparator": "gt",
        "threshold": 0.01,
        "unit": "ratio",
        "started_at": STARTED,
        "resolved_at": None,
        "value": 0.042,
    }
    values.update(changes)
    return values


def test_a_firing_mail_names_the_rule_value_threshold_and_environment_with_no_link():
    subject, body = alert_email(alert(), "local", "firing")
    assert subject == "[Critical] Run failure rate above 10 % is firing in local"
    assert body == (
        "Rule: Run failure rate above 10 %\n"
        "State: Firing since 14:02 UTC\n"
        "Signal: Run failure rate\n"
        "Value: 4.2 % (threshold above 1 %)\n"
        "Environment: local\n"
        "\n" + FOOTER + "\n"
    )
    assert "http" not in body and "://" not in body


def test_resolved_and_repeat_mails_say_so():
    resolved = alert(resolved_at=STARTED + timedelta(minutes=18), value=0.0, severity="warning")
    subject, body = alert_email(resolved, "local", "resolved")
    assert subject == "Resolved: Run failure rate above 10 % in local"
    assert "State: Resolved at 14:20 UTC\n" in body and "Value: 0 % (threshold above 1 %)\n" in body
    subject, _ = alert_email(alert(severity="info"), "local", "repeat")
    assert subject == "[Info] Run failure rate above 10 % is still firing in local"


def test_values_read_in_their_unit():
    waiting = alert(signal="queue.oldest_ready_seconds", unit="seconds", comparator="gte", threshold=300, value=412.5)
    assert "Value: 412.5 s (threshold at or above 300 s)\n" in alert_email(waiting, "local", "firing")[1]
    workers = alert(signal="workers.online", unit="count", comparator="lt", threshold=1, value=0.0)
    body = alert_email(workers, "local", "firing")[1]
    assert "Signal: Workers online\n" in body and "Value: 0 (threshold below 1)\n" in body
    assert set(ALERT_SIGNAL_NAMES) == set(ALERT_SIGNALS)


def test_the_subject_keeps_80_characters_of_the_rule_name():
    name = "R" * 120
    subject, body = alert_email(alert(rule_name=name), "local", "firing")
    assert subject == "[Critical] " + "R" * 80 + " is firing in local"
    assert "Rule: " + name + "\n" in body


def test_an_approval_mail_names_the_target_intent_and_expiry_never_the_reason():
    subject, body = approval_email("local-docker", "scale_workers", STARTED + timedelta(minutes=15), "local")
    assert subject == "A change request awaits approval in local"
    assert body == (
        "Target: local-docker\n"
        "Change: Scale workers\n"
        "Expires: 14:17 UTC\n"
        "\n"
        "Open Studio › Operate › Clusters › Approvals to review it.\n"
    )


RECIPIENTS = [f"{index:064x}" for index in range(10)]


def usage(**changes) -> Usage:
    values = {"rule_channel_recent": False, "rule_hour": 0, "environment_hour": 0, "recipients_hour": {}}
    values.update(changes)
    return Usage(**values)


def test_60_messages_an_hour_per_environment_counted_per_recipient():
    assert ENVIRONMENT_HOUR == 60
    # Six sends to ten recipients fit; the seventh doesn't.
    assert admit("test", RECIPIENTS, usage(environment_hour=50)) == RECIPIENTS
    assert admit("test", RECIPIENTS, usage(environment_hour=51)) is None
    assert admit("approval", RECIPIENTS[:1], usage(environment_hour=60)) is None


def test_40_messages_per_rule_per_hour():
    assert RULE_HOUR == 40
    assert admit("firing", RECIPIENTS, usage(rule_hour=30)) == RECIPIENTS
    assert admit("repeat", RECIPIENTS, usage(rule_hour=31)) is None
    # Test and approval messages belong to no rule.
    assert admit("test", RECIPIENTS, usage(rule_hour=40)) == RECIPIENTS


def test_one_message_per_rule_and_channel_every_15_minutes():
    for kind in ("firing", "resolved", "repeat"):
        assert admit(kind, RECIPIENTS, usage(rule_channel_recent=True)) is None
    assert admit("test", RECIPIENTS, usage(rule_channel_recent=True)) == RECIPIENTS


def test_20_messages_per_recipient_per_hour():
    assert RECIPIENT_HOUR == 20
    busy = {RECIPIENTS[0]: 20, RECIPIENTS[1]: 19}
    assert admit("firing", RECIPIENTS[:3], usage(recipients_hour=busy)) == RECIPIENTS[1:3]
    assert admit("firing", RECIPIENTS[:1], usage(recipients_hour=busy)) is None


def test_a_mail_whose_outcome_is_unclear_is_never_sent_again():
    assert outcome(["unknown", "rejected"], 1) == ("failed", "mail_unknown")
    assert outcome(["accepted", "accepted"], 1) == ("sent", None)
    assert outcome(["accepted", "rejected"], 1) == ("sent", "mail_refused")
    # A recipient the connection refuses, or an owner who can't send, ends it at once.
    assert outcome(["rejected"], 1, final="recipient_not_allowed") == ("failed", "recipient_not_allowed")


def test_a_transient_failure_is_retried_at_most_three_times_after_1_5_and_15_minutes():
    assert [outcome(["rejected"], attempts) for attempts in (1, 2, 3, 4)] == [
        ("pending", "mail_refused"),
        ("pending", "mail_refused"),
        ("pending", "mail_refused"),
        ("failed", "mail_refused"),
    ]
    now = STARTED
    assert due(0, None, now) and not due(0, now - timedelta(seconds=90), now)
    assert due(0, now - timedelta(minutes=2), now)
    for attempts, minutes in ((1, 1), (2, 5), (3, 15)):
        assert not due(attempts, now - timedelta(minutes=minutes) + timedelta(seconds=1), now)
        assert due(attempts, now - timedelta(minutes=minutes), now)


def test_each_message_has_one_request_per_recipient_and_attempt():
    notification = uuid4()
    first = request_id(notification, 0, 0)
    assert first == request_id(notification, 0, 0)
    assert len({first, request_id(notification, 1, 0), request_id(notification, 0, 1), request_id(uuid4(), 0, 0)}) == 4


@pytest.mark.parametrize("kind", ["firing", "resolved", "repeat"])
def test_no_alert_mail_names_a_selector_or_a_recipient(kind):
    body = alert_email(alert(), "local", kind)[1]
    assert "@" not in body and "selector" not in body
