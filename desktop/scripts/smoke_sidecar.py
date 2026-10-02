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

"""Exercise a frozen sidecar without an installed Python package or exposed bootstrap."""

import argparse
import asyncio
import http.cookiejar
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


async def check(binary: Path, expected_version: str | None = None) -> None:
    child = await asyncio.create_subprocess_exec(
        str(binary.resolve()),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": ""},
    )
    try:
        assert child.stdout is not None
        line = await asyncio.wait_for(child.stdout.readline(), timeout=45)
        if not line:
            # This invocation never supplies a profile or performs login; keep only
            # bounded offline startup stderr, never the bootstrap or environment.
            diagnostic = Path("desktop/work/frozen-startup-error.txt")
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            assert child.stderr is not None
            stderr = await asyncio.wait_for(child.stderr.read(65536), timeout=5)
            diagnostic.write_bytes(stderr)
            raise RuntimeError("Frozen host exited before readiness; see desktop/work/frozen-startup-error.txt")
        boot = json.loads(line)
        parsed = urlsplit(boot["origin"])
        assert boot["type"] == "weave-desktop-ready" and parsed.hostname == "127.0.0.1"
        assert parsed.scheme == "http" and parsed.port and not parsed.query and not parsed.fragment
        origin = boot["origin"]
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

        def request(path: str, payload: dict[str, object] | None = None, csrf: str | None = None) -> bytes:
            headers = {"Origin": origin, "Content-Type": "application/json"}
            if csrf:
                headers["X-Weave-CSRF"] = csrf
            data = json.dumps(payload).encode() if payload is not None else None
            with opener.open(urllib.request.Request(origin + path, data=data, headers=headers), timeout=10) as response:
                return bytes(response.read())

        assert b"<html" in request("/").lower()
        assert not json.loads(request("/studio/session"))["paired"]
        pair = json.loads(request("/studio/session", {"code": boot["pairing_code"]}))
        assert pair["paired"] and pair["mode"] == "offline"
        if expected_version is not None:
            assert pair["version"] == expected_version, "Frozen host version does not match the release"
        assert not json.loads(request("/studio/connection"))["configured"]
        assert (
            json.loads(
                request("/studio/local/validate", {"format": "yaml", "source": "not: [valid"}, pair["csrfToken"])
            )["validationOk"]
            is False
        )
        assert child.stdin is not None
        child.stdin.write(b"STOP\n")
        await child.stdin.drain()
        assert await asyncio.wait_for(child.wait(), timeout=10) == 0
        print("Frozen host readiness, assets, pairing, connection assistant, compiler and owned shutdown passed")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    asyncio.run(check(args.binary, args.expected_version))


if __name__ == "__main__":
    main()
