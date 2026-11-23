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

"""When a call to the platform is worth repeating, and how long to wait first.

Workers and runners poll the platform for work. While its API restarts, their calls are refused, drop or time out,
and waiting is the only fix; a refusal such as 401, 403 or a refused release is not fixed by waiting.

A request the API refused before admitting it (429 with a capacity code) ran nothing. The API lets a client send it
again when it is a read or a change that carries an idempotency key, honoring Retry-After (docs/reference/api.md,
"Idempotency keys"). ``Resend`` and ``send_admitted`` are the one policy the SDK client and the cluster commands share.
"""

import asyncio
import math
import random as _random
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from firefly_weave.definitions.models import CAPACITY_CODES
from firefly_weave.sdk.errors import ContractError, TransportError, WeaveError

# Statuses that ask a caller to come back later: rate limiting, or a gateway or API that is not serving yet.
RETRY_LATER = frozenset({429, 502, 503, 504})
# A problem code as the platform mints it, such as WV-TRANSPORT or WV-WIRE-VERSION, and how long it may be.
CODE = re.compile(r"WV-[A-Z0-9]+(?:-[A-Z0-9_]+)*")
LONGEST_CODE = 64
# Retry-After as RFC 9110 delay-seconds: whole seconds, here at most six digits. A date or any other value is not
# read, and counts as a wait without end.
DELAY_SECONDS = re.compile(r"[0-9]{1,6}")
# Replaced in tests.
clock = time.monotonic
wait = asyncio.sleep
jitter = _random.random


def _chain(error: BaseException) -> list[BaseException]:
    """``error``, then each error it was raised from or while handling, innermost last."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and not any(current is seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def unavailable(error: BaseException) -> bool:
    """Whether ``error`` says the platform could not be reached, or asked to be called again later."""
    # Imported here so that the worker module stays importable without an HTTP client installed.
    import httpcore
    import httpx

    # httpx and httpcore raise a certificate they cannot verify as a ConnectError raised from it. Waiting never makes
    # the certificate trusted, so it is a wrong address or an interception, not an outage. Other TLS errors, such as
    # an unexpected end of the stream, also happen while an API restarts.
    if any(isinstance(link, ssl.SSLCertVerificationError) for link in _chain(error)):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRY_LATER
    if isinstance(error, ContractError):
        # WeaveClient refused the request or the answer itself: a destination its policy does not permit, another
        # wire version, or an answer outside the contract. Waiting changes none of them.
        return False
    if isinstance(error, TransportError):
        # WeaveClient reports every failed httpx call as WV-TRANSPORT 503 and keeps that call's error as the
        # context, so the call's error decides: a refused connection is an outage, an unsupported protocol is not.
        failure = error.__cause__ or error.__context__
        return failure is not None and unavailable(failure)
    if isinstance(error, WeaveError):
        return error.status in RETRY_LATER
    # The default httpx transport raises httpx's errors; the pinned private-origin transport passes httpcore's on.
    return isinstance(
        error,
        (
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpcore.NetworkError,
            httpcore.TimeoutException,
            httpcore.RemoteProtocolError,
        ),
    )


def kind(error: BaseException) -> str:
    """The failure's type and its HTTP status or problem code, with the type of the innermost error it came from, for
    example ``ConnectError (gaierror)``; never a message, which can carry a URL, an address or a token."""
    import httpx

    if isinstance(error, httpx.HTTPStatusError):
        return f"{type(error).__name__} (HTTP {error.response.status_code})"
    if isinstance(error, WeaveError):
        # The code comes from the server and is printed in logs and terminals, where a newline or an escape sequence
        # could forge a line: anything the platform would not mint is printed as WV-HTTP.
        code = error.code if len(error.code) <= LONGEST_CODE and CODE.fullmatch(error.code) else "WV-HTTP"
        # WeaveClient keeps the error of a call that failed as the context of its WV-TRANSPORT or WV-WIRE error.
        failure = error.__cause__ or error.__context__
        return f"{code} (HTTP {error.status})" + ("" if failure is None else f" from {kind(failure)}")
    # A refused connection, a name that does not resolve and a TLS failure all arrive as ConnectError.
    innermost = _chain(error)[-1]
    return type(error).__name__ + ("" if innermost is error else f" ({type(innermost).__name__})")


def retry_delay(failures: int, first: float, cap: float, *, random: Callable[[], float] = _random.random) -> float:
    """The wait after ``failures`` failed calls in a row: ``first``, doubling with each failure up to ``cap``.

    Each wait is spread over its upper half, so that callers that lost the platform together do not all call it
    again at the same moment.
    """
    return min(first * 2.0 ** min(failures - 1, 64), cap) * (0.5 + random() / 2)


def raise_if_cancelled(error: BaseException) -> None:
    """Raise CancelledError, instead of handling ``error``, while this task has a cancellation it has not honored.

    A client library that closes its connection when it is cancelled can raise its own error in place of the
    CancelledError; a loop that took that error for an unreachable platform would keep running after it was cancelled.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError() from error


def retry_after(value: str | None) -> float | None:
    """The seconds a ``Retry-After`` header asks the caller to wait: None without one, and infinity when it holds
    anything but whole seconds the client reads (a date, for example), so no caller comes back sooner than asked."""
    if value is None:
        return None
    value = value.strip(" \t")
    return float(value) if DELAY_SECONDS.fullmatch(value) else math.inf


def not_admitted(error: BaseException) -> bool:
    """Whether the API refused the request before admitting it: HTTP 429 with a capacity code, so nothing ran."""
    return isinstance(error, WeaveError) and error.status == 429 and error.code in CAPACITY_CODES


@dataclass(frozen=True)
class Resend:
    """How a request the API did not admit is sent again before its refusal stands.

    ``sends`` counts every send, the first one included; None allows as many as ``seconds`` does. No pause ends later
    than ``seconds`` after the first send. A pause is never shorter than the API's Retry-After; it starts at ``first``,
    doubles up to ``longest``, and is spread up to a quarter longer so that callers refused together do not all come
    back at once. A Retry-After longer than ``longest``, or one the client cannot read, lets the refusal stand.
    """

    sends: int | None
    seconds: float
    first: float = 0.5
    longest: float = 5.0

    def __post_init__(self) -> None:
        if (
            (self.sends is not None and self.sends < 1)
            or not 0 < self.seconds < math.inf
            or not 0 < self.first <= self.longest < math.inf
        ):
            raise ValueError("A bounded resend policy is required")

    def pause(self, refusals: int, retry_after: float | None, spread: float) -> float | None:
        """The wait after ``refusals`` refusals in a row, with ``spread`` in [0, 1); None when the refusal stands."""
        if retry_after is not None and retry_after > self.longest:
            return None
        backoff = min(self.first * 2.0 ** min(refusals - 1, 64), self.longest)
        return max(backoff, retry_after or 0.0) * (1 + spread / 4)


# The API's bound for a client that sends a refused request again: four sends in all.
CAPACITY_RESEND = Resend(sends=4, seconds=15)


async def send_admitted[T](
    send: Callable[[], Awaitable[T]],
    policy: Resend,
    *,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[object]] | None = None,
    stopped: Callable[[], bool] | None = None,
) -> T:
    """Await ``send()``, and send it again while the API refuses it for capacity, as ``policy`` allows.

    The last refusal reaches the caller. Only a request that is safe to send twice belongs here: a read, or a change
    the API deduplicates. ``monotonic`` and ``sleep`` default to this module's clock and wait. ``stopped`` is asked
    after each pause, for example whether the client was closed meanwhile: once it is true, the last refusal stands.
    """
    now = monotonic or clock
    pause = sleep or wait
    deadline = now() + policy.seconds
    refusals = 0
    while True:
        try:
            return await send()
        except WeaveError as error:
            raise_if_cancelled(error)
            refusals += 1
            if not not_admitted(error) or (policy.sends is not None and refusals >= policy.sends):
                raise
            delay = policy.pause(refusals, error.retry_after, jitter())
            if delay is None or now() + delay > deadline:
                raise
            refused = error
        await pause(delay)
        if stopped is not None and stopped():
            raise refused
