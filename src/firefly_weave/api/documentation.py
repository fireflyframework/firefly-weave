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

"""Opt-in Swagger UI backed by the same offline contract as published reference docs."""

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from firefly_weave.contracts.openapi import export_openapi

# Pin bytes as well as version: the browser rejects changed CDN assets.
_ASSETS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.1/"
_CSS_INTEGRITY = "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW"
_JS_INTEGRITY = "sha384-ZPehFMQommnnuaZ4rpxgkgTT2DKFVp4hZC/7pLit+9Lek9T1YGSo23eHFbvNkXkw"


def install_documentation(app: Starlette) -> None:
    """Mount documentation after native operation coverage has been checked."""
    spec = export_openapi()

    async def openapi(request: Request) -> JSONResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        return JSONResponse({**spec, "servers": [{"url": root or "/"}]}, headers={"Cache-Control": "no-store"})

    async def swagger(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        config = json.dumps(
            {
                "url": root + "/openapi.json",
                "dom_id": "#swagger-ui",
                "deepLinking": True,
                "filter": True,
                "displayOperationId": True,
                "defaultModelsExpandDepth": 0,
                "defaultModelRendering": "model",
                "persistAuthorization": False,
                "validatorUrl": None,
                "queryConfigEnabled": False,
                "tryItOutEnabled": False,
                "withCredentials": False,
            }
        ).replace("<", "\\u003c")
        html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Firefly Weave API explorer</title>
<link rel="stylesheet" href="{_ASSETS}swagger-ui.css" integrity="{_CSS_INTEGRITY}" crossorigin="anonymous">
<style>
body {{ margin: 0; background: #EEF4F0; font-family: system-ui, sans-serif; }}
header {{ padding: 24px max(20px, calc((100vw - 1460px) / 2)); background: #173D34; color: white; }}
header h1 {{ margin: 0 0 8px; font-size: 26px; }} header p {{ margin: 6px 0; line-height: 1.5; }}
header a {{ color: #8ee3dc; }} .swagger-ui .info {{ margin: 24px 0; }}
</style></head><body>
<header><h1>Firefly Weave · API explorer</h1>
<p>Browse operations, authorize with a current access token, then choose Try it out.</p>
<p>Requests run against this API. Write operations change real data. Tokens stay in this page's memory.</p>
<p><a href="https://fireflyframework.github.io/firefly-weave/guides/api-playground/">
First API request and authentication guide</a></p>
</header><div id="swagger-ui"></div>
<noscript>Enable JavaScript to use Swagger UI, or download this API's /openapi.json.</noscript>
<script id="weave-docs-config" type="application/json">{config}</script>
<script src="{_ASSETS}swagger-ui-bundle.js" integrity="{_JS_INTEGRITY}" crossorigin="anonymous"></script>
<script>
const config = JSON.parse(document.getElementById("weave-docs-config").textContent);
config.presets = [SwaggerUIBundle.presets.apis];
config.requestInterceptor = request => {{
    const destination = new URL(request.url, window.location.href);
    if (destination.origin !== window.location.origin) throw new Error("Only this API origin is allowed");
    request.credentials = "omit";
    return request;
}};
SwaggerUIBundle(config);
</script></body></html>'''
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    app.router.routes.extend(
        [
            Route("/docs", swagger, methods=["GET"], name="weave.docs"),
            Route("/openapi.json", openapi, methods=["GET"], name="weave.openapi"),
        ]
    )
    app.state.weave_docs_enabled = True
