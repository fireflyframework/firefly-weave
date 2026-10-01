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

"""Local demonstration effect target with independently durable idempotency receipts."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def accept(path, key, payload):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", key)
        or type(payload) is not dict
        or set(payload) != {"customer"}
        or type(payload["customer"]) is not str
    ):
        raise ValueError("Invalid effect request")
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"receipt": "accepted", "customer": payload["customer"]}
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE IF NOT EXISTS effects (operation_key TEXT PRIMARY KEY, "
            "fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
        )
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute("SELECT fingerprint,result FROM effects WHERE operation_key=?", (key,)).fetchone()
        if existing:
            if existing[0] != fingerprint:
                raise ValueError("Idempotency conflict")
            return json.loads(existing[1])
        database.execute("INSERT INTO effects VALUES(?,?,?)", (key, fingerprint, json.dumps(result)))
    return result


def serve(database, host, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404, {"ready": self.path == "/health"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/effect" or not 0 < length <= 65536:
                    raise ValueError("Invalid request")
                output = accept(database, self.headers.get("Idempotency-Key", ""), json.loads(self.rfile.read(length)))
                self.respond(200, output)
            except (ValueError, UnicodeError, OSError):
                self.respond(409, {"error": "Effect rejected"})

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with HTTPServer((host, port), Handler) as server:
        print("Local idempotent receiver ready", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    os.umask(0o077)
    if args.database.is_symlink() or not args.database.parent.is_dir():
        parser.exit(1, "Use an owned regular receipt database in an existing private directory.\n")
    serve(args.database, args.host, args.port)
