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

"""Independent local durable receipt receiver; no state is reset between crashes."""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit


class EffectReceiver:
    def __init__(self, database, *, signing_key=None):
        self.signing_key = signing_key
        self.database = database
        descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with sqlite3.connect(database) as store:
            store.execute("PRAGMA synchronous=FULL")
            store.execute(
                "CREATE TABLE attempts(id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL, accepted INTEGER)"
            )
            store.execute("CREATE TABLE effects(operation_key TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self.tasks = set()
        self.errors = []

    async def open(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=16384)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(20):
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("ascii").split("\r\n")
                method, path, _ = lines[0].split(" ")
                headers = {key.lower(): value for line in lines[1:] if line for key, value in [line.split(": ", 1)]}
                if method == "GET" and path.startswith("/accepted?"):
                    key = parse_qs(urlsplit(path).query)["key"][0]
                    with sqlite3.connect(self.database) as store:
                        found = store.execute("SELECT 1 FROM effects WHERE operation_key=?", (key,)).fetchone()
                    output = {"accepted": found is not None}
                else:
                    assert method == "POST" and path in {"/effect", "/effect-loss"}
                    key = headers["idempotency-key"]
                    length = int(headers["content-length"])
                    assert len(key) <= 256 and 0 <= length <= 1048576
                    with sqlite3.connect(self.database) as store:
                        cursor = store.execute("INSERT INTO attempts(operation_key,accepted) VALUES(?,0)", (key,))
                        attempt = cursor.lastrowid
                        store.commit()
                    body = await reader.readexactly(length)
                    json.loads(body)
                    if self.signing_key is not None:
                        stamp = headers["x-weave-timestamp"]
                        expected = hmac.new(
                            self.signing_key.encode(), stamp.encode() + b"." + body, hashlib.sha256
                        ).hexdigest()
                        assert abs(int(stamp) - time.time()) <= 300
                        assert hmac.compare_digest(expected, headers["x-weave-signature"])
                        assert headers["authorization"] == "Bearer " + self.signing_key
                        assert headers["x-weave-event-id"] == key
                    with sqlite3.connect(self.database) as store:
                        store.execute("INSERT OR IGNORE INTO effects VALUES(?,?)", (key, body))
                        assert (
                            store.execute("SELECT body FROM effects WHERE operation_key=?", (key,)).fetchone()[0]
                            == body
                        )
                        store.execute("UPDATE attempts SET accepted=1 WHERE id=?", (attempt,))
                        store.commit()
                    if path == "/effect-loss":
                        return
                    output = {}
                raw = json.dumps(output).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(raw)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + raw
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            # Deliberately killed clients may disappear between headers and body.
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    def snapshot(self):
        with sqlite3.connect(self.database) as store:
            attempts = [
                dict(zip(("id", "operation_key", "accepted"), row, strict=True))
                for row in store.execute("SELECT id,operation_key,accepted FROM attempts ORDER BY id")
            ]
            effects = [row[0] for row in store.execute("SELECT operation_key FROM effects ORDER BY operation_key")]
        return {"attempts": attempts, "effects": effects}

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in self.tasks.copy():
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        assert not self.errors, "Independent receiver failed; inspect retained protocol evidence"
