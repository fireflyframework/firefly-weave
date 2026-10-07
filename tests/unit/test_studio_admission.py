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

"""Asset navigation remains available while expensive Studio requests are busy."""

import asyncio

import httpx
import pytest
from starlette.responses import Response

from firefly_weave.studio.host import StudioBoundary, make_studio_app
from firefly_weave.studio.service import BODY_LIMIT, StudioOptions, StudioProfile, StudioService

ORIGIN = "http://127.0.0.1:8766"
CODE = "test-pairing-code"


def options(tmp_path, **kwargs):
    (tmp_path / "index.html").write_text("<!doctype html><title>Studio</title>")
    (tmp_path / "main.js").write_text("console.log('Studio loaded');")
    return StudioOptions(origin=ORIGIN, assets=tmp_path, pairing_code=CODE, **kwargs)


async def test_busy_proxy_does_not_reject_real_navigation_or_assets(tmp_path):
    release = asyncio.Event()
    entered = asyncio.Event()
    running = 0

    async def upstream(request):
        nonlocal running
        running += 1
        if running == 8:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    app = make_studio_app(
        options(
            tmp_path,
            profile=StudioProfile(name="Test", base_url="https://api.example"),
            token_provider=lambda: "test-token",
            transport=httpx.MockTransport(upstream),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as browser,
    ):
        paired = await browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
        assert paired.status_code == 200
        pending = [asyncio.create_task(browser.get("/studio/api/api/v1/identity")) for _ in range(8)]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            rejected = await browser.get("/studio/api/api/v1/identity")
            assert rejected.status_code == 429
            assert rejected.json()["code"] == "WV-STUDIO-BUSY"
            navigation = await browser.get("/operations/targets/example")
            assert navigation.status_code == 200
            script = await browser.get("/main.js")
            assert script.status_code == 200
            assert "Studio loaded" in script.text
            assert "default-src 'self'" in script.headers["content-security-policy"]
            assert (await browser.head("/main.js")).status_code == 200
            assert (await browser.get("/missing.js")).status_code == 404
            assert (await browser.get("/main.js", headers={"Origin": "https://evil.example"})).status_code == 403
            oversized = await browser.request("GET", "/main.js", content=b"x" * (BODY_LIMIT + 1))
            assert oversized.status_code == 413
        finally:
            release.set()
            await asyncio.gather(*pending)


async def test_parallel_assets_queue_without_taking_api_slots(tmp_path):
    service = StudioService(options(tmp_path))
    release = asyncio.Event()
    entered = asyncio.Event()
    running = maximum = 0

    async def downstream(scope, receive, send):
        nonlocal running, maximum
        if scope["path"] != "/studio/session":
            running += 1
            maximum = max(maximum, running)
            if running == 8:
                entered.set()
            try:
                await release.wait()
            finally:
                running -= 1
        await Response("ok")(scope, receive, send)

    app = StudioBoundary(downstream, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as browser:
        pending = [asyncio.create_task(browser.get(f"/chunk-{i}.js")) for i in range(12)]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await browser.get("/studio/session")).status_code == 200
            release.set()
            replies = await asyncio.gather(*pending)
            assert all(reply.status_code == 200 for reply in replies)
            assert maximum == 8
        finally:
            release.set()
            await asyncio.gather(*pending)
            await service.close()


@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
async def test_abandoned_asset_wait_does_not_leak_or_release_an_owned_slot(tmp_path, monkeypatch, abandon):
    from firefly_weave.studio import host

    monkeypatch.setattr(host, "STATIC_WAIT_SECONDS", 0.01 if abandon == "timeout" else 10)
    service = StudioService(options(tmp_path))
    release = asyncio.Event()
    entered = asyncio.Event()
    running = 0

    async def downstream(scope, receive, send):
        nonlocal running
        running += 1
        if running == 8:
            entered.set()
        try:
            await release.wait()
            await Response("ok")(scope, receive, send)
        finally:
            running -= 1

    app = StudioBoundary(downstream, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as browser:
        pending = [asyncio.create_task(browser.get(f"/chunk-{i}.js")) for i in range(8)]
        waiting = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            waiting = asyncio.create_task(browser.get("/waiting.js"))
            if abandon == "timeout":
                response = await asyncio.wait_for(waiting, 5)
                assert response.status_code == 429
                assert response.json()["code"] == "WV-STUDIO-BUSY"
            else:
                await asyncio.sleep(0)
                assert not waiting.done()
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            assert running == 8
            assert app.asset_slots.locked()
            release.set()
            assert all(response.status_code == 200 for response in await asyncio.gather(*pending))
            # All original permits, and no extra one, return after the holders finish.
            for _ in range(8):
                await asyncio.wait_for(app.asset_slots.acquire(), 1)
            assert app.asset_slots.locked()
            for _ in range(8):
                app.asset_slots.release()
            assert (await browser.get("/after.js")).status_code == 200
        finally:
            if waiting is not None and not waiting.done():
                waiting.cancel()
            release.set()
            await asyncio.gather(*pending)
            await service.close()
