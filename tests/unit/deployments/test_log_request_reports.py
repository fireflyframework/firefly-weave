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

"""What the server keeps of a runner's log report, and what a log request reads as over its hour."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from firefly_weave.contracts.deployments import LogLine, MemberLog
from firefly_weave.deployments.log_requests import request_view, stored_members

T0 = datetime(2026, 11, 19, 10, 41, 52, tzinfo=UTC)
# Secret-shaped values are built from parts at run time, so the repository holds no secret-shaped text.
TOKEN = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "c2lnbmF0dXJlLXBhcnQ"
PASSWORD = "hunter" + "-22-" + "correct-horse"


def line(text: str, seconds: int = 0, stream: str = "stdout") -> LogLine:
    return LogLine(at=T0 + timedelta(seconds=seconds), stream=stream, text=text)


def test_the_server_redacts_what_the_runner_left_in():
    report = [
        MemberLog(
            member="weave-worker-7c9f-x2k4",
            lines=[
                line(f"WARN smtp-relay answered 550; Authorization: Bearer {TOKEN}", 0, "stderr"),
                line(f"connecting with password={PASSWORD} to billing-db", 1),
                line("Claimed task fetch for run 1c0d7f8a", 2),
            ],
        )
    ]
    members, truncated = stored_members(report)
    texts = [item.text for item in members[0].lines]
    assert all(TOKEN not in text and PASSWORD not in text for text in texts)
    assert "[redacted]" in texts[0] and "[redacted]" in texts[1]
    assert texts[2] == "Claimed task fetch for run 1c0d7f8a"
    assert [item.stream for item in members[0].lines] == ["stderr", "stdout", "stdout"]
    assert truncated is False


def test_a_line_longer_than_2_kib_is_cut_at_a_character_and_says_so():
    members, truncated = stored_members([MemberLog(member="w1", lines=[line("é" * 2000)])])
    kept = members[0].lines[0].text
    assert len(kept.encode()) <= 2048 and kept == "é" * 1024
    assert truncated is True


def test_only_the_newest_lines_within_256_kib_are_kept_and_each_member_stays_in_time_order():
    # Two members of 100 lines of 2,000 bytes each: 400,000 bytes, more than the 262,144 a snapshot keeps.
    first = MemberLog(member="w1", lines=[line(f"{n:04d}" + "a" * 1996, 2 * n) for n in range(100)])
    second = MemberLog(member="w2", lines=[line(f"{n:04d}" + "b" * 1996, 2 * n + 1) for n in range(100)])
    members, truncated = stored_members([first, second])
    kept = [item for member in members for item in member.lines]
    assert truncated is True
    assert sum(len(item.text.encode()) for item in kept) <= 262144
    assert len(kept) == 131
    # The newest lines stay, the oldest go, and each member's lines read in time order.
    assert members[0].lines[-1].text.startswith("0099") and members[1].lines[-1].text.startswith("0099")
    newest_dropped = max(item.at for member in (first, second) for item in member.lines if item not in kept)
    assert all(item.at > newest_dropped for item in kept)
    for member in members:
        assert [item.at for item in member.lines] == sorted(item.at for item in member.lines)


def test_a_snapshot_within_its_bounds_is_kept_whole():
    report = [MemberLog(member="w1", lines=[line("one", 0), line("two", 1)]), MemberLog(member="w2", lines=[])]
    assert stored_members(report) == (report, False)


def row(**values) -> dict:
    created = values.pop("created_at", T0)
    return {
        "id": uuid4(),
        "target_id": uuid4(),
        "component": "fixture-worker",
        "members": ["w1"],
        "since_seconds": 900,
        "tail_lines": 200,
        "state": "pending",
        "failure": None,
        "created_at": created,
        "expires_at": created + timedelta(hours=1),
        "read_at": None,
        **values,
    }


SNAPSHOT = {"members": [{"member": "w1", "lines": [line("ready").model_dump(mode="json")]}], "truncated": True}


def test_a_request_waits_for_its_runner_for_two_minutes_then_reads_as_unanswered():
    for state in ("pending", "claimed"):
        waiting = request_view(row(state=state), None, T0 + timedelta(seconds=119))
        assert (waiting.state, waiting.failure, waiting.members) == ("pending", None, [])
        late = request_view(row(state=state), None, T0 + timedelta(seconds=120))
        assert (late.state, late.failure) == ("failed", "no_answer")


def test_a_ready_snapshot_shows_its_lines_until_its_hour_ends():
    read = T0 + timedelta(seconds=5)
    ready = request_view(row(state="ready", read_at=read), SNAPSHOT, T0 + timedelta(minutes=59))
    assert (ready.state, ready.read_at, ready.truncated) == ("ready", read, True)
    assert [item.text for item in ready.members[0].lines] == ["ready"]
    expired = request_view(row(state="ready", read_at=read), SNAPSHOT, T0 + timedelta(hours=1))
    assert (expired.state, expired.members, expired.truncated) == ("expired", [], False)


def test_a_failed_request_says_why_and_requested_members_are_echoed():
    failed = request_view(row(state="failed", failure="unknown_component"), None, T0 + timedelta(seconds=3))
    assert (failed.state, failed.failure, failed.requested_members) == ("failed", "unknown_component", ["w1"])
