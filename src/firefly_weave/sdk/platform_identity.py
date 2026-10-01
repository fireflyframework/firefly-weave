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

"""Refresh the owned development identity inside the isolated server environment."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from firefly_weave.sdk.platform import _private_directory, _write


async def refresh(directory: Path) -> None:
    import httpx

    from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig
    from firefly_weave.sdk.deployment import read_file, strict_json

    _private_directory(directory)
    provider = ProviderConfig.model_validate(json.loads(os.environ["WEAVE_OIDC_PROVIDERS"])[0])
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            provider.issuer + "/protocol/openid-connect/token",
            data={"grant_type": "client_credentials"},
            auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]),
        )
        response.raise_for_status()
        token = response.json()["access_token"]
    identity = await OIDCVerifier(provider).verify(token)
    destination = directory / "host-token.json"
    if destination.exists() and strict_json(read_file(destination, 65536, private=True))["subject"] != identity.subject:
        raise ValueError("The verified identity changed")
    _write(destination, {"access_token": token, "subject": identity.subject}, replace=destination.exists())


if __name__ == "__main__":
    try:
        asyncio.run(refresh(Path(sys.argv[1])))
    except Exception:
        raise SystemExit("Identity refresh failed; check the owned provider and private configuration.") from None
