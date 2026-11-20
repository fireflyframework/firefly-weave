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

"""Container log lines as the runner reports them: timed, redacted, cut to 2 KiB, and only the newest.

The adapters read with fixed commands that put each line's RFC 3339 time first (`docker logs --timestamps`,
`kubectl logs --timestamps`). Nothing here logs or keeps what a container printed beyond the report it builds.
"""

from collections.abc import Sequence

from firefly_weave.contracts.deployments import (
    LOG_LINE_BYTES,
    LOG_MEMBERS,
    LogLine,
    LogStream,
    RunnerLogFailure,
    cut_line,
)
from firefly_weave.deployment_runner.runtime import RunnerPolicyError, observed_time
from firefly_weave.operations.log_redaction import redact_text


class LogRefused(Exception):
    """The runner reads nothing for this request, and says why."""

    def __init__(self, failure: RunnerLogFailure) -> None:
        super().__init__(failure)
        self.failure: RunnerLogFailure = failure


def chosen(observed: Sequence[str], asked: Sequence[str]) -> tuple[list[str], bool]:
    """The members to read: those asked for that the runner observed, or the first five it observed; True when
    observed members beyond five were left out. Refuses when none remains."""
    if asked:
        known = set(observed)
        hosts = [host for host in asked if host in known][:LOG_MEMBERS]
        more = False
    else:
        hosts, more = list(observed[:LOG_MEMBERS]), len(observed) > LOG_MEMBERS
    if not hosts:
        raise LogRefused("no_members")
    return hosts, more


def _lines(raw: bytes, stream: LogStream) -> tuple[list[LogLine], bool]:
    result, cut = [], False
    for piece in raw.split(b"\n"):
        if not piece:
            continue
        stamp, _, rest = piece.partition(b" ")
        try:
            at = observed_time(stamp.decode("ascii"))
        except (UnicodeDecodeError, RunnerPolicyError):
            at = None
        if at is None:
            # Every line a fixed command prints starts with its time; one without it is part of a cut line.
            cut = True
            continue
        redacted = redact_text(rest.decode(errors="replace").removesuffix("\r"), limit=LOG_LINE_BYTES)
        text, shortened = cut_line(redacted.text)
        cut = cut or redacted.truncated or shortened
        result.append(LogLine(at=at, stream=stream, text=text))
    return result, cut


def member_lines(stdout: bytes, stderr: bytes | None, *, tail: int) -> tuple[list[LogLine], bool]:
    """One member's last `tail` lines in time order; `stderr` None means stdout holds both streams (`output`). True
    when a line was cut or left out."""
    if stderr is None:
        lines, cut = _lines(stdout, "output")
    else:
        out, out_cut = _lines(stdout, "stdout")
        err, err_cut = _lines(stderr, "stderr")
        lines, cut = sorted(out + err, key=lambda line: line.at), out_cut or err_cut
    return lines[-tail:], cut or len(lines) > tail
