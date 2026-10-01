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

"""Outer bounded body admission and value-free error/header handling before native filters."""

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import debug_execution, execute_pure, request_execution

BODY_SLOTS = 16
BODY_BYTES = 128 * 1024 * 1024
CONTROL_BODY_SLOTS = 4
CONTROL_BODY_BYTES = 32 * 1024 * 1024
DEBUG_SLOTS = 1


@dataclass(frozen=True)
class TransportPolicy:
    json_bytes: int = 6_356_998
    debug_bytes: int = 67_108_864
    raw_bytes: int = 1_048_576
    idle_seconds: float = 5
    total_seconds: float = 30

    def __post_init__(self) -> None:
        if not (
            0 < self.json_bytes <= 6_356_998
            and 0 < self.debug_bytes <= 67_108_864
            and 0 < self.raw_bytes <= 1_048_576
            and 0 < self.idle_seconds <= 5
            and 0 < self.total_seconds <= 30
        ):
            raise ValueError("Invalid bounded transport policy")

    def budget(self, path: str) -> tuple[int, bool]:
        if path.startswith(("/provider-ingress/", "/webhooks/")):
            return self.raw_bytes, False
        if "/debug/sessions" in path:
            return self.debug_bytes, True
        return self.json_bytes, False


class _Reservations:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = self.bytes = self.debug = 0
        self.control_requests = self.control_bytes = 0

    def acquire(self, size: int, debug: bool, control: bool = False) -> bool:
        with self.lock:
            count = self.control_requests if control else self.requests - self.control_requests
            used = self.control_bytes if control else self.bytes - self.control_bytes
            slots = CONTROL_BODY_SLOTS if control else BODY_SLOTS - CONTROL_BODY_SLOTS
            budget = CONTROL_BODY_BYTES if control else BODY_BYTES - CONTROL_BODY_BYTES
            if count >= slots or used + size > budget or (debug and self.debug >= DEBUG_SLOTS):
                return False
            self.control_requests += int(control)
            self.control_bytes += size if control else 0
            self.requests += 1
            self.bytes += size
            self.debug += int(debug)
            return True

    def release(self, size: int, debug: bool, control: bool = False) -> None:
        with self.lock:
            self.control_requests -= int(control)
            self.control_bytes -= size if control else 0
            self.requests -= 1
            self.bytes -= size
            self.debug -= int(debug)


_reservations = _Reservations()
_IDS = (b"x-request-id", b"x-correlation-id", b"x-transaction-id")
_STRIP = {*_IDS, b"x-tenant-id", b"baggage", b"tracestate", b"traceparent"}
_TRACE = re.compile(rb"00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)")


def safe_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    result = [(name, value) for name, value in headers if name.lower() not in _STRIP]
    for key in _IDS:
        values = [value for name, value in headers if name.lower() == key]
        valid = None
        if len(values) == 1 and len(values[0]) == 36:
            try:
                candidate = str(UUID(values[0].decode("ascii"))).encode()
                if candidate == values[0]:
                    valid = candidate
            except (ValueError, UnicodeError):
                pass
        result.append((key, valid or str(uuid4()).encode()))
    traces = [value for name, value in headers if name.lower() == b"traceparent"]
    if len(traces) == 1:
        match = _TRACE.fullmatch(traces[0])
        if match and int(match[1], 16) and int(match[2], 16):
            result.append((b"traceparent", traces[0]))
    return result


def scan_json_structure(body: bytearray, *, debug: bool) -> None:
    depth = tokens = 0
    quoted = escaped = scalar = False
    max_depth, max_tokens = (136, 4_001_024) if debug else (40, 201_024)
    for byte in body:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted = True
            scalar = False
            tokens += 1
        elif byte in (123, 91):
            depth += 1
            tokens += 1
            scalar = False
        elif byte in (125, 93):
            depth -= 1
            scalar = False
        elif byte in (44, 58, 32, 9, 10, 13):
            scalar = False
        elif not scalar:
            scalar = True
            tokens += 1
        if depth > max_depth or tokens > max_tokens:
            raise CatalogError(413, "WV-REQUEST-LIMIT", "Request structure exceeds allocation policy")


class BodyBoundary:
    def __init__(self, app: ASGIApp, policy: TransportPolicy | None = None) -> None:
        self.app = app
        self.policy = policy or TransportPolicy()
        self.routes = tuple(
            (name, operation.method, re.compile(re.sub(r"\{[^}]+\}", "[^/]+", path)))
            for name, operation in OPERATIONS.items()
            for path in {operation.path, operation.canonical_path}
        )

    async def problem(self, send: Send, status: int, code: str, request_id: str) -> None:
        body = json.dumps(
            {
                "status": status,
                "code": code,
                "message": "Request unavailable",
                "request_id": request_id,
                "diagnostics": [],
            }
        ).encode()
        headers = [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode())]
        headers.extend([(b"x-weave-wire-version", b"weave/api-v1"), (b"x-weave-request-id", request_id.encode())])
        if status == 429:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        telemetry = getattr(getattr(scope.get("app"), "state", None), "telemetry_service", None)
        if scope["type"] != "http" or telemetry is None:
            await self._serve(scope, receive, send)
            return
        path = scope.get("path", "")
        operation = next(
            (
                name
                for name, method, pattern in self.routes
                if len(path) <= 8192 and scope.get("method") == method and pattern.fullmatch(path)
            ),
            "other",
        )
        traceparent = next(
            (
                value.decode("ascii", errors="ignore")
                for name, value in safe_headers(scope.get("headers", []))
                if name == b"traceparent"
            ),
            None,
        )
        status = 500
        started = time.monotonic()

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            with telemetry.span(operation, traceparent=traceparent):
                await self._serve(scope, receive, observed_send)
        finally:
            telemetry.record(
                "request",
                operation=operation,
                status="ok" if status < 400 else "rejected" if status < 500 else "failed",
                duration=time.monotonic() - started,
            )

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        app = scope.get("app")
        compatibility = getattr(getattr(app, "state", None), "compatibility", None)
        path = scope.get("path", "")
        restricted_safe = (
            scope.get("method") in {"GET", "HEAD", "OPTIONS"}
            or path.endswith(("/cancel", "/disable", "/revoke"))
            or "/operations/compatibility" in path
        )
        if compatibility is not None and not compatibility.ready and not restricted_safe:
            await self.problem(send, 503, "WV-COMPATIBILITY", request_id)
            return
        headers = scope.get("headers", [])
        if sum(len(k) + len(v) for k, v in headers) > 16_384:
            await self.problem(send, 431, "WV-REQUEST-HEADERS", request_id)
            return
        budget, debug = self.policy.budget(scope.get("path", ""))
        lengths = [value for name, value in headers if name.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 10)):
            await self.problem(send, 400, "WV-REQUEST-HEADERS", request_id)
            return
        if lengths and int(lengths[0]) > budget:
            await self.problem(send, 413, "WV-REQUEST-LIMIT", request_id)
            return
        if any(name.lower() == b"content-encoding" for name, _ in headers):
            await self.problem(send, 415, "WV-REQUEST-ENCODING", request_id)
            return
        scope = {**scope, "headers": safe_headers(headers)}
        has_body = scope.get("method") not in {"GET", "HEAD", "OPTIONS"} or bool(lengths and int(lengths[0]))
        control = not debug and (
            scope.get("method") in {"GET", "HEAD", "OPTIONS"}
            or scope.get("path", "").endswith(("/cancel", "/disable", "/revoke"))
            or "/operations/compatibility" in scope.get("path", "")
        )
        reserved = has_body and _reservations.acquire(budget, debug, control)
        if has_body and not reserved:
            await self.problem(send, 429, "WV-REQUEST-CAPACITY", request_id)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            downstream_receive = receive
            if has_body:
                body = bytearray()
                try:
                    async with asyncio.timeout(self.policy.total_seconds):
                        while True:
                            async with asyncio.timeout(self.policy.idle_seconds):
                                message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            chunk = message.get("body", b"")
                            if len(chunk) > budget - len(body):
                                await self.problem(send, 413, "WV-REQUEST-LIMIT", request_id)
                                return
                            body.extend(chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    await self.problem(send, 408, "WV-REQUEST-TIMEOUT", request_id)
                    return
                delivered = False

                async def buffered_receive() -> Message:
                    nonlocal delivered
                    if delivered:
                        return await receive()
                    delivered = True
                    data = bytes(body)
                    body.clear()
                    return {"type": "http.request", "body": data, "more_body": False}

                downstream_receive = buffered_receive
            try:
                async with request_execution(control=control), debug_execution(enabled=debug):
                    if has_body and not scope.get("path", "").startswith(("/provider-ingress/", "/webhooks/")):
                        await execute_pure(lambda: scan_json_structure(body, debug=debug))
                    await self.app(scope, downstream_receive, tracked_send)
            except CatalogError as error:
                if started:
                    raise
                await self.problem(send, error.status, error.code, request_id)
        except Exception:
            logging.getLogger("weave.operations").error("Request failed", extra={"error_code": "WV-INTERNAL"})
            if not started:
                await self.problem(send, 500, "WV-INTERNAL", request_id)
            else:
                # The response may already contain effect acknowledgments. Never replace it with a new success/error.
                raise RuntimeError("Response interrupted") from None
        finally:
            if reserved:
                _reservations.release(budget, debug, control)
