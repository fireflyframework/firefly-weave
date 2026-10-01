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

# Launch a standalone local runtime

This guide starts a new local PostgreSQL and Keycloak installation, installs the
built Weave wheel, and runs a workflow through the public API. It retains every
created database and volume. Use a dedicated local Docker context and unused
ports. The identity configuration is for localhost development; production OIDC
requires trusted HTTPS endpoints and explicit client and audience policy.

You need Python 3.12 or later, `uv`, Docker with Compose, and a local Unix-socket
Docker context you own. Run these commands from the source distribution root in
a Bash or Zsh terminal. The [offline quickstart](../quickstart.md) works without
these services. Keep this terminal open so the variables below stay available.

## 1. Reserve the installation and install exact artifacts

Choose your owned context. Inspect it before continuing; its Docker endpoint must
be local. These commands use `colima-weave-tests` as an example, without switching
the globally selected context. If it is not your owned context, set the variable
to your own local context before running a command.

```sh
export WEAVE_DOCKER_CONTEXT=colima-weave-tests
docker context inspect "$WEAVE_DOCKER_CONTEXT"
docker --context "$WEAVE_DOCKER_CONTEXT" info >/dev/null

umask 077
mkdir -p .local
export WEAVE_LAUNCH_ID="weave-local-$(python3 -c 'from uuid import uuid4; print(uuid4().hex[:12])')"
export WEAVE_WORK_DIR="$PWD/.local/$WEAVE_LAUNCH_ID"
mkdir -m 700 "$WEAVE_WORK_DIR"
uv run --locked --no-editable --all-extras python scripts/prepare_release.py \
  --output "$WEAVE_WORK_DIR/release"
uv venv --python 3.12 "$WEAVE_WORK_DIR/runtime"
uv pip install --python "$WEAVE_WORK_DIR/runtime/bin/python" --require-hashes \
  -r "$WEAVE_WORK_DIR/release/images/server-requirements.txt"
uv pip install --python "$WEAVE_WORK_DIR/runtime/bin/python" --no-deps \
  "$WEAVE_WORK_DIR/release/artifacts/$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/release/release.json"))["wheel"])')"
export WEAVE_PYTHON="$WEAVE_WORK_DIR/runtime/bin/python"
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main version --output json
```

Expected: release preparation prints wheel and sdist SHA-256 values; the final
command reports the installed Weave version and language/IR versions. The
`release.json` receipt identifies the exact artifact, including when two builds
have the same package version. Protect the whole work directory: later steps
write credentials inside it.

## 2. Create fresh database and identity services

The defaults below use PostgreSQL 55434, Keycloak 18080, API 8080, and the later
container API 8081. Preconfigure the port variables to select unused ports; the
commands preserve those choices. Do not reuse a port or volume belonging to
another installation. The runtime setup helper accepts only canonical
`http://localhost:<port>` origins with ports 1024 through 65535.

```sh
export WEAVE_POSTGRES_PORT="${WEAVE_POSTGRES_PORT:-55434}"
export WEAVE_KEYCLOAK_PORT="${WEAVE_KEYCLOAK_PORT:-18080}"
export WEAVE_API_PORT="${WEAVE_API_PORT:-8080}"
export WEAVE_CONTAINER_API_PORT="${WEAVE_CONTAINER_API_PORT:-8081}"
export WEAVE_POSTGRES_VOLUME="$WEAVE_LAUNCH_ID-postgres"
export WEAVE_KEYCLOAK_VOLUME="$WEAVE_LAUNCH_ID-keycloak"
export WEAVE_KEYCLOAK_TEST_URL="http://localhost:$WEAVE_KEYCLOAK_PORT"
python3 - <<'PY'
import os, socket
for name in ("WEAVE_POSTGRES_PORT", "WEAVE_KEYCLOAK_PORT", "WEAVE_API_PORT", "WEAVE_CONTAINER_API_PORT"):
    with socket.socket() as check:
        check.bind(("127.0.0.1", int(os.environ[name])))
print("Selected local ports are available")
PY
python3 scripts/setup-local.py --output "$WEAVE_WORK_DIR/postgres.env"
python3 scripts/setup-identity.py --output "$WEAVE_WORK_DIR/identity.env"
set -a
source "$WEAVE_WORK_DIR/postgres.env"
source "$WEAVE_WORK_DIR/identity.env"
set +a

docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml config --quiet
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml up --detach --no-recreate --wait \
  --wait-timeout 180 postgres keycloak
```

Expected: PostgreSQL is healthy and Keycloak is running. `WEAVE_CONTAINER_API_PORT`
is reserved for the later API container; it has no readiness endpoint until that container starts. Verify OIDC discovery
before creating a runtime database:

```sh
"$WEAVE_PYTHON" - <<'PY'
import os, time, httpx
url = os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave/.well-known/openid-configuration"
for attempt in range(90):
    try:
        response = httpx.get(url, timeout=2, trust_env=False)
        if response.status_code == 200:
            assert response.json()["issuer"] == os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave"
            print("Keycloak discovery is ready")
            break
    except httpx.HTTPError:
        pass
    time.sleep(1)
else:
    raise SystemExit("Keycloak discovery did not become ready")
PY
"$WEAVE_PYTHON" scripts/setup-runtime.py --output "$WEAVE_WORK_DIR/runtime.env"
set -a
source "$WEAVE_WORK_DIR/runtime.env"
set +a
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main admin migrate
```

Expected: a fresh retained runtime database is provisioned, then `Weave schema is
current`. The helper creates separate application and scheduler login credentials
and applies migrations explicitly. It does not overwrite a database or existing
credential file. Keep migration and identity-administrator credentials out of API
and worker process environments. Existing Keycloak realm imports are skipped;
changing a secret file does not update a retained realm.

## 3. Verify and bootstrap one local identity

For this local walkthrough, the `weave-host` service account is the explicit
bootstrap administrator and receives scoped author/operator grants in step 5.
In a shared deployment, separate bootstrap administration from application
identities. Provider role names never grant Weave domain authority.

Acquire a short-lived access token and verify its signature, issuer, audience,
client and token class before using its subject for bootstrap:

```sh
"$WEAVE_PYTHON" - <<'PY'
import asyncio, json, os
from pathlib import Path
import httpx
from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig
async def main():
    config = ProviderConfig.model_validate(json.loads(os.environ["WEAVE_OIDC_PROVIDERS"])[0])
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(config.issuer + "/protocol/openid-connect/token",
            data={"grant_type": "client_credentials"},
            auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]))
        response.raise_for_status()
        token = response.json()["access_token"]
    identity = await OIDCVerifier(config).verify(token)
    path = Path(os.environ["WEAVE_WORK_DIR"]) / "host-token.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"access_token": token, "subject": identity.subject}, stream)
    print("Verified host identity; private token file written")
asyncio.run(main())
PY
export WEAVE_HOST_SUBJECT="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/host-token.json"))["subject"])')"
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main admin bootstrap \
  --provider local-keycloak --issuer "$WEAVE_KEYCLOAK_TEST_URL/realms/weave" \
  --subject "$WEAVE_HOST_SUBJECT" --kind application \
  --output "$WEAVE_WORK_DIR/bootstrap.json"
```

Expected: `Identity linked; private receipt written`. No token is printed. The
receipt contains the actual local principal ID used by the next step. A bootstrap
conflict means the identity is already linked; inspect that installation instead
of deleting or resetting it. If the token expires while you work, acquire a fresh
verified token into a new file and pass that file to subsequent commands; do not
repeat bootstrap.

## 4. Launch and check the API

In a second terminal, return to this repository and set `WEAVE_WORK_DIR` to the
installation path printed by `printf '%s\n' "$WEAVE_WORK_DIR"` in the first
terminal. Set `WEAVE_API_PORT` to the same selected value as in the first terminal.
Source only `runtime.env` in that terminal, then start the installed API:

```sh
set -a
source "$WEAVE_WORK_DIR/runtime.env"
set +a
env -u WEAVE_MIGRATION_DATABASE_URL \
  "$WEAVE_WORK_DIR/runtime/bin/python" -I -m uvicorn \
  firefly_weave.main:create_application --factory --host 127.0.0.1 --port "${WEAVE_API_PORT:?Set the selected API port}"
```

Keep the API in the foreground. In the first terminal:

```sh
export WEAVE_API_URL="http://127.0.0.1:$WEAVE_API_PORT"
"$WEAVE_PYTHON" - <<'PY'
import os, time, httpx
for attempt in range(60):
    try:
        response = httpx.get(os.environ["WEAVE_API_URL"] + "/health/ready", timeout=2, trust_env=False)
        if response.status_code == 200:
            print("API is ready")
            break
    except httpx.HTTPError:
        pass
    time.sleep(1)
else:
    raise SystemExit("API readiness timed out; inspect the API terminal")
PY
```

Readiness must succeed before any provisioning request. An incompatible schema
fails startup; apply the matching wheel's explicit migration command rather than
letting a runtime process migrate its own database.

## 5. Grant scope, publish, activate and run

The checked-in example uses public HTTP exclusively. It creates a uniquely named
tenant, grants `tenant_admin` to the bootstrap principal for that tenant, creates
its project/environment, and grants project `developer` plus environment
`deployer`, `operator` and `viewer`. It captures all IDs from actual JSON responses.
It then publishes and activates a pure transform workflow and starts its first run.

```sh
"$WEAVE_PYTHON" examples/first_run.py --api-url "$WEAVE_API_URL" \
  --token-file "$WEAVE_WORK_DIR/host-token.json" \
  --bootstrap-receipt "$WEAVE_WORK_DIR/bootstrap.json" \
  --output "$WEAVE_WORK_DIR/first-run.json"
```

Expected JSON includes `status: "succeeded"`, `output: {"message": "Hello, Weave"}`,
and the tenant, project, environment, activation and run IDs. This workflow needs
no remote worker or external effect. The new private receipt is never overwritten.
A failed attempt may retain resources; use a new output filename when retrying.

Read that same run through the typed SDK:

```sh
"$WEAVE_PYTHON" - <<'PY'
import asyncio, json, os
from pathlib import Path
from uuid import UUID
from firefly_weave.contracts.access import Scope
from firefly_weave.sdk.client import WeaveClient
root = Path(os.environ["WEAVE_WORK_DIR"])
receipt = json.loads((root / "first-run.json").read_text())
token = json.loads((root / "host-token.json").read_text())["access_token"]
async def main():
    async with WeaveClient(os.environ["WEAVE_API_URL"], lambda: token,
            Scope.model_validate(receipt["scope"])) as client:
        run = await client.read_run(UUID(receipt["run_id"]))
        print(run.state.status, run.state.output)
asyncio.run(main())
PY
```

Expected: `succeeded {'message': 'Hello, Weave'}`. Read the same run through the
CLI using IDs from the receipt and the private access token:

```sh
export WEAVE_BASE_URL="$WEAVE_API_URL"
export WEAVE_ACCESS_TOKEN="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/host-token.json"))["access_token"])')"
export WEAVE_TENANT_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/first-run.json"))["scope"]["tenant_id"])')"
export WEAVE_PROJECT_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/first-run.json"))["scope"]["project_id"])')"
export WEAVE_ENVIRONMENT_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/first-run.json"))["scope"]["environment_id"])')"
export WEAVE_RUN_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/first-run.json"))["run_id"])')"
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main runs read "$WEAVE_RUN_ID"
unset WEAVE_ACCESS_TOKEN
```

Expected: the same run ID, `state.status: "succeeded"`, and declared output. The
[native contract reference](../reference/native-openapi.md) explains the generated
schema and shared API/SDK/CLI surface.

## 6. Add workers and package the runtime

A remote worker needs its own verified identity link, a release admitted by a
scoped deployer, and a current `worker` grant restricted to that release and task
references. It receives HTTP credentials and handler configuration, never database
credentials. A native executor additionally needs explicit server-side executor
configuration and trusted connector credentials. Neither obtains authority merely
by starting an image.

Continue with the ordered [packaging and Compose commands](../operations/deployment.md)
using this installation's `release` directory. They distinguish local image IDs,
worker-only shutdown and full runtime shutdown. The [worker protocol](../reference/worker-protocol.md)
defines lease fencing, finite token drain and ambiguous external effects.

## 7. Stop safely and retain data

Press **Ctrl-C in the API terminal** and wait for the process to exit. This stops
that runtime's background loops and active work according to their shutdown
bounds. It does not undo an external effect. Then stop only this installation's
Compose services:

```sh
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml stop --timeout 30
```

The containers, volumes, databases and credentials remain. Reuse the exact same
installation variables and secret files when restarting; do not regenerate them.
Do not use `down --volumes` as a shutdown command. Before a backup, fence **all**
writers and follow [backup and restore](../operations/backup-restore.md).
