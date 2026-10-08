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

"""Acme API fixture for acceptance journeys: customers, orders, approvals and a webhook sink.

Standard library only. Port 8080 serves the API on the fixture egress network; port
8081 serves the control API (faults, request journal, reset) and is published on
loopback only. The journal records the SHA-256 of X-Api-Key, never the key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DATA: dict[str, Any] = json.loads(Path(__file__).with_name("data.json").read_text(encoding="utf-8"))
ORIGIN = "http://acme.acceptance.test:8080"
MAX_BODY = 65536
CUSTOMER = re.compile(r"/customers/([A-Z0-9-]{1,32})")
ORDERS = re.compile(r"/customers/([A-Z0-9-]{1,32})/orders")
STATUS = re.compile(r"/orders/([A-Z0-9-]{1,32})/status")
SINK = re.compile(r"/sink/([a-z0-9-]{1,64})")


def _json(schema: str) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}}


_ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


def openapi() -> dict[str, Any]:
    """The OpenAPI 3.1 document for "import OpenAPI": five operations on the fixture origin."""
    text = {"type": "string"}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Acme API", "version": "1.0.0"},
        "servers": [{"url": ORIGIN}],
        "components": {
            "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
            "schemas": {
                "Customer": {
                    "type": "object",
                    "required": ["id", "name", "country", "email"],
                    "properties": {"id": text, "name": text, "country": text, "email": text},
                },
                "Order": {
                    "type": "object",
                    "required": ["id", "amount", "currency", "country"],
                    "properties": {"id": text, "amount": {"type": "number"}, "currency": text, "country": text},
                },
                "Orders": {
                    "type": "object",
                    "required": ["customerId", "items"],
                    "properties": {
                        "customerId": text,
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}},
                    },
                },
                "OrderStatus": {
                    "type": "object",
                    "required": ["orderId", "status"],
                    "properties": {
                        "orderId": text,
                        "status": {"type": "string", "enum": ["shipped", "on_hold", "cancelled"]},
                    },
                },
                "Approval": {
                    "type": "object",
                    "required": ["customerId", "lines"],
                    "properties": {"customerId": text, "lines": {"type": "array", "items": text}},
                },
                "Notification": {
                    "type": "object",
                    "required": ["customerId", "message"],
                    "properties": {"customerId": text, "message": text},
                },
                "Receipt": {"type": "object", "required": ["id", "status"], "properties": {"id": text, "status": text}},
            },
        },
        "paths": {
            "/customers/{id}": {
                "get": {
                    "operationId": "getCustomer",
                    "summary": "Get a customer",
                    "security": [{"apiKey": []}],
                    "parameters": [_ID],
                    "responses": {"200": {"description": "The customer", **_json("Customer")}},
                }
            },
            "/customers/{id}/orders": {
                "get": {
                    "operationId": "listCustomerOrders",
                    "summary": "List a customer's orders",
                    "security": [{"apiKey": []}],
                    "parameters": [_ID],
                    "responses": {"200": {"description": "The orders", **_json("Orders")}},
                }
            },
            "/orders/{id}/status": {
                "get": {
                    "operationId": "getOrderStatus",
                    "summary": "Get an order's status",
                    "parameters": [_ID],
                    "responses": {"200": {"description": "The status", **_json("OrderStatus")}},
                }
            },
            "/approvals": {
                "post": {
                    "operationId": "createApproval",
                    "summary": "Record an approval",
                    "requestBody": {"required": True, **_json("Approval")},
                    "responses": {"201": {"description": "Received", **_json("Receipt")}},
                }
            },
            "/notifications": {
                "post": {
                    "operationId": "createNotification",
                    "summary": "Notify a customer",
                    "requestBody": {"required": True, **_json("Notification")},
                    "responses": {"201": {"description": "Received", **_json("Receipt")}},
                }
            },
        },
    }


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class State:
    """Journal, faults and receipt counter shared by the API and control servers."""

    def __init__(self, key_sha256: str) -> None:
        self.key_sha256 = key_sha256
        self.lock = threading.Lock()
        self.journal: list[dict[str, Any]] = []
        self.faults: dict[str, dict[str, int]] = {}
        self.receipts = 0

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.journal.append(entry)

    def take_fault(self, path: str) -> int | None:
        with self.lock:
            fault = self.faults.get(path)
            if fault is None:
                return None
            fault["count"] -= 1
            if fault["count"] <= 0:
                del self.faults[path]
            return fault["status"]

    def receipt(self, prefix: str) -> str:
        with self.lock:
            self.receipts += 1
            return f"{prefix}-{self.receipts}"

    def authorized(self, key_sha256: str | None) -> bool:
        return key_sha256 is not None and bool(self.key_sha256) and hmac.compare_digest(key_sha256, self.key_sha256)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AcmeFixture/1"
    sys_version = ""
    state: State

    def log_message(self, format: str, *args: Any) -> None:
        # No access log: request lines and headers stay out of container logs.
        return

    def _read_body(self) -> bytes | None:
        """The request body, or None when it is over 64 KiB (drained when at most 256 KiB)."""
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit():
            self.close_connection = True
            return None
        size = int(length)
        if size > MAX_BODY:
            if size <= 4 * MAX_BODY:
                self.rfile.read(size)
            else:
                self.close_connection = True
            return None
        return self.rfile.read(size)

    def _send(self, status: int, value: Any = None) -> None:
        data = b"" if value is None else json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        if value is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)


class ApiHandler(_Handler):
    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PUT(self) -> None:
        self._serve()

    def do_PATCH(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def _serve(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body() if self.command in {"POST", "PUT", "PATCH"} else b""
        key = self.headers.get("X-Api-Key")
        key_sha256 = None if key is None else _digest(key.encode())
        injected = self.state.take_fault(path)
        if injected is not None:
            status, value = injected, {"error": "injected"}
        elif body is None:
            status, value = 413, {"error": "The request body is over 64 KiB."}
        else:
            status, value = self._route(path, body, key_sha256)
        self.state.record(
            {
                "method": self.command,
                "path": path,
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "key_sha256": key_sha256,
                "status": status,
                "body_sha256": _digest(body) if body else None,
            }
        )
        self._send(status, value)

    def _route(self, path: str, body: bytes, key_sha256: str | None) -> tuple[int, Any]:
        if self.command in {"GET", "HEAD"}:
            if path == "/":
                return 200, {"service": "acme"}
            if path == "/openapi.json":
                return 200, openapi()
            for pattern, kind in ((ORDERS, "orders"), (CUSTOMER, "customer")):
                match = pattern.fullmatch(path)
                if match is None:
                    continue
                if not self.state.authorized(key_sha256):
                    return 401, {"error": "X-Api-Key is missing or wrong."}
                customer = match[1]
                if customer not in DATA["customers"]:
                    return 404, {"error": "Unknown customer."}
                if kind == "customer":
                    return 200, DATA["customers"][customer]
                return 200, {"customerId": customer, "items": DATA["orders"].get(customer, [])}
            match = STATUS.fullmatch(path)
            if match is not None:
                status = DATA["statuses"].get(match[1])
                if status is None:
                    return 404, {"error": "Unknown order."}
                return 200, {"orderId": match[1], "status": status}
            return 404, {"error": "Not found."}
        if self.command == "POST":
            if path in {"/approvals", "/notifications"}:
                try:
                    payload = json.loads(body or b"null")
                except ValueError:
                    payload = None
                if not isinstance(payload, dict):
                    return 400, {"error": "Send a JSON object."}
                return 201, {"id": self.state.receipt("A" if path == "/approvals" else "N"), "status": "received"}
            if SINK.fullmatch(path):
                return 204, None
            return 404, {"error": "Not found."}
        return 405, {"error": "Method not allowed."}


def _fault(body: bytes) -> tuple[str, int, int] | None:
    try:
        value = json.loads(body)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {"status", "count", "path"}:
        return None
    status, count, path = value["status"], value["count"], value["path"]
    if (
        type(status) is not int
        or not 400 <= status <= 599
        or type(count) is not int
        or not 1 <= count <= 1000
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 256
    ):
        return None
    return path, status, count


class ControlHandler(_Handler):
    def do_GET(self) -> None:
        if self.path != "/_control/journal":
            self._send(404, {"error": "Not found."})
            return
        with self.state.lock:
            entries = list(self.state.journal)
        self._send(200, {"entries": entries})

    def do_POST(self) -> None:
        body = self._read_body()
        if body is None:
            self._send(413, {"error": "The request body is over 64 KiB."})
        elif self.path == "/_control/reset":
            with self.state.lock:
                self.state.journal.clear()
                self.state.faults.clear()
            self._send(204)
        elif self.path == "/_control/faults":
            fault = _fault(body)
            if fault is None:
                self._send(400, {"error": "Send {status: 400-599, count: 1-1000, path: /…}."})
                return
            with self.state.lock:
                self.state.faults[fault[0]] = {"status": fault[1], "count": fault[2]}
            self._send(204)
        else:
            self._send(404, {"error": "Not found."})


def make_servers(
    *, host: str, api_port: int, control_port: int, key_sha256: str
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, State]:
    state = State(key_sha256)
    api = ThreadingHTTPServer((host, api_port), type("AcmeApi", (ApiHandler,), {"state": state}))
    control = ThreadingHTTPServer((host, control_port), type("AcmeControl", (ControlHandler,), {"state": state}))
    return api, control, state


def main() -> None:
    key = os.environ.get("ACME_API_KEY_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise SystemExit("ACME_API_KEY_SHA256 must be the SHA-256 hex digest of the API key.")
    api, control, _ = make_servers(host="0.0.0.0", api_port=8080, control_port=8081, key_sha256=key)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    api.serve_forever()


if __name__ == "__main__":
    main()
