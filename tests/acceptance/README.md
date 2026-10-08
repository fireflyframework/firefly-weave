<!--
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Acceptance journeys

Acceptance journeys prove Firefly Weave against real services: a Docker platform
from `weave platform up`, its PostgreSQL and Keycloak, the Acme API fixture and
the Studio host built from this checkout. Nothing is mocked, and no secret is
needed: every password and API key is generated for the run.

## Run the pr profile

You need Docker with Compose 2.30 or later (Colima on macOS), uv and Node 24.
On macOS, keep the checkout inside your home folder: Colima shares only that
folder with containers, and the platform mounts files from the checkout.

```sh
# Install the locked Python and Studio environments and Playwright's Chromium.
uv sync --locked --all-extras --group docs
(cd studio && npm ci && npx playwright install chromium)
# Run every stage of the pr profile against a Docker context you own.
uv run --no-sync python scripts/acceptance.py --profile pr --docker-context colima-weave-tests
```

Expected, at the end: `Acceptance pr run RUN_ID: all requested stages passed.`
The first run builds the server image and takes about 25 minutes.

## Stages and evidence

| Stage | What it does | Timeout |
| --- | --- | --- |
| prepare | Checks tools, pulls the images in [versions.toml](versions.toml) by digest, builds the Acme fixture and the server image from the inputs `weave platform up` uses, builds Studio | 30 min |
| up | On Linux CI with `--block-egress`, drops container traffic to public addresses; then journey J0 | 25 min |
| run | The other journey tests and the Playwright suites of the profile | 35 min (pr) |
| collect | `weave platform status` and container logs | 5 min |
| scan | Fails when any generated secret appears in evidence or private files | 5 min |
| down | Removes the run's containers, networks and volumes, then audits that nothing is left | 10 min |

Each run writes `build/acceptance/RUN_ID/`. `private/` holds the platform, the
people files (0600) and Playwright traces; never share it. `evidence/` holds
`acceptance.json`, which validates against
[evidence.schema.json](evidence.schema.json), plus step screenshots and logs.
Run one stage of an existing run with `--stages up --run-id RUN_ID`; keep
everything for debugging with `--keep`, then remove it with
`--stages down --run-id RUN_ID`.

## Enable a journey step

[journeys.toml](journeys.toml) maps each step (`J0.3`) and check
(`J0.3/owner-roles`) to the product capabilities that enable it;
`ai-models-screen|compose-operations` holds when either has landed. A step whose
capabilities have not landed reports `skipped` with them, never `passed`. When the
change that delivers a capability merges:

1. Set the capability to `true` under `[capabilities]`.
2. Add or change the steps and checks it enables.
3. Add their tests: a pytest step carries `@pytest.mark.journey_step("J0.3")`,
   and a check runs inside its step through `run.check("J0.3/owner-roles", body)`.

`tests/unit/test_acceptance_journeys.py` fails when an enabled step or check has
no test.

## Fixtures

The [Acme API fixture](fixtures/acme_api/server.py) answers as
`http://acme.acceptance.test:8080` on the installation's egress network, which
`weave platform up --allow-private-origin` creates. Its control API (faults,
request journal, reset) is published on loopback only, and its journal keeps the
SHA-256 of `X-Api-Key`, never the key.
