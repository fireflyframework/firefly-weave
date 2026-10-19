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
| run | The other pytest journeys, the Playwright suites of the profile, then the Studio journeys (`studio/playwright.acceptance.config.ts`) | 35 min (pr) |
| collect | `weave platform status` and container logs | 5 min |
| scan | Fails when any generated secret appears in evidence, private files or any Studio browser storage; records step J9.4 | 5 min |
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

## Studio journeys

Studio journeys live in `studio/tests/acceptance/*.spec.ts` and run with
`studio/playwright.acceptance.config.ts`: one worker, no retries, traces only on
failure and only under `private/`. The orchestrator writes `RUN_DIR/enablement.json`
from [journeys.toml](journeys.toml); without `WEAVE_ACC_RUN_DIR` every journey
skips. A journey is one test; each step runs through
`j.step("J1.1", async () => { … })` and each check through
`j.check("J9.2/execute-in-local", async () => { … })`, which record the same
statuses as the pytest harness. People sign in with `j.signIn("builder")` in
their own browser context, through the real Studio host and the identity
provider's pages. When a journey ends, the harness keeps every context's
browser storage and requests under `private/` for the scan, checks that every
request stayed on the Studio host and the identity provider, and removes each
platform through Studio, which signs out and deletes the stored sign-in.
Sign-ins left by a failed test are deleted only when the credential store was
read before the run started; otherwise nothing is deleted.

A journey spec imports only named values from `harness.ts` (which re-exports
`expect` and the helpers journeys use) and types from `@playwright/test`. Journey
code, and every declaration it reaches in other files, may not intercept
requests, inject sign-in state, control the clock, force clicks, dispatch
synthetic events or open its own browser, context or page, and every
`page.evaluate` is a read-only audit with a note before it
(`studio/tests/acceptance-lint.ts`, which parses the code with the TypeScript
compiler). At run time the harness backs this up: Playwright's own `browser`,
`context`, `page` and `request` fixtures throw when used, and every context the
harness opens refuses request interception, injected headers, credentials and
cookies, stored state, offline mode, page scripts and browser protocol sessions;
its `request` only reads with GET.

## People and API sessions

`J0` creates the owner, builder, approver, operator and viewer with generated
passwords in `private/people/` (0600). `weave platform user` grants every role
but `developer` and `tenant_admin` on the demo environment, and an environment
grant does not cover project operations such as reading the workflow catalog,
so J0.4 also grants the viewer its `viewer` role on the demo project
(`PROJECT_ROLES` in `test_j0_bring_up.py`). A person whose roles must reach
project resources needs the same. pytest journeys call the API as a person
with `async with run.client("builder") as client:`, which signs in through the
identity provider's pages with PKCE every time it is called. Each client keeps
its tokens in its own file under `private/sessions/`, removed when the client
closes, never in the system credential store. Steps the spec runs through the
CLI use `async with run.cli("owner") as weave:` and `weave("human-groups", "put",
request={…})`: the typed `weave` commands with the same private token file
(`--credential-store file`) and the demo workspace as explicit flags.

## Viewports and evidence

Studio steps are recorded per viewport: every Studio journey at 1440x900, and
J1, J3 and J7 also at 600x500; nightly and release runs record both sizes for
every Studio journey. `acceptance.json` has one entry per journey and viewport;
a pytest step counts for every viewport of its journey. Step screenshots go to
`evidence/screens/JOURNEY/VIEWPORT/`.

## Authorization matrix

[authorization.toml](authorization.toml) lists every operation added since the
0.1.0a14 release with the capability it needs and one request that is safe to
send as anyone. `tests/contracts/test_authorization_matrix.py` fails when a new
operation has no row. Journey step J9.1 sends every row as each person and
expects exactly `403 WV-FORBIDDEN` where the person's roles lack the capability.

## Fixtures

The [Acme API fixture](fixtures/acme_api/server.py) answers as
`http://acme.acceptance.test:8080` on the installation's egress network, which
`weave platform up --allow-private-origin` creates. Its control API (faults,
request journal, reset) is published on loopback only, and its journal keeps the
SHA-256 of `X-Api-Key`, never the key.
