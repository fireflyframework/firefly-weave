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

# Package and deploy an exact local artifact

Start with the [standalone walkthrough](../guides/standalone.md). This page uses
its `WEAVE_WORK_DIR`, `WEAVE_PYTHON`, `WEAVE_DOCKER_CONTEXT`, `WEAVE_LAUNCH_ID` and
fresh `release` directory. PostgreSQL and Keycloak must already be ready, and the
installed API must have completed a first public run. Use Docker Compose 2.30 or later and a local Docker context
with a Unix endpoint; remote Docker endpoints and Docker sockets inside the API
are unsupported by `weave worker deploy`.

A local `sha256:` image ID identifies local image bytes. It is not a registry
manifest digest, signature, live-provider certification, or automatic runtime
attestation. Release admission and current scoped grants remain explicit.

## Build the server from the prepared wheel

The preparation command builds one wheel and sdist and exports separate base,
worker, server, Teams and Kafka closures from `uv.lock`. The Python and uv images
are pinned by digest. Every image installs hash-locked dependencies, then the exact
wheel with `--no-deps`; no editable checkout or source bind mount is used.

```sh
docker --context "$WEAVE_DOCKER_CONTEXT" build --target server \
  --iidfile "$WEAVE_WORK_DIR/server-image.id" \
  "$WEAVE_WORK_DIR/release/images"
export WEAVE_SERVER_IMAGE="$(cat "$WEAVE_WORK_DIR/server-image.id")"
docker --context "$WEAVE_DOCKER_CONTEXT" image inspect \
  --format '{{.Id}} {{.Config.User}}' "$WEAVE_SERVER_IMAGE"
docker --context "$WEAVE_DOCKER_CONTEXT" run --name "$WEAVE_LAUNCH_ID-server-inventory" \
  --network none --read-only --entrypoint python "$WEAVE_SERVER_IMAGE" -I -c \
  'import json, pathlib; print(json.loads(pathlib.Path("/opt/weave/build.json").read_text())["wheel_sha256"])'
```

Expected: the inspect command prints the selected image ID and `65532:65532`.
The inventory command prints the wheel SHA from `release/release.json` and exits.
Its stopped container remains. The `teams-server` and `kafka-server` Docker targets
use their own complete closures when those integrations are required. The worker
Dockerfile independently installs the worker closure; it does not inherit the
server environment.

## Prepare a worker context

The example manifest declares `example-record@1.0.0`. Its entrypoint requires an
owned HTTP effect endpoint that accepts `Idempotency-Key`, retains receipts, and
returns `{"receipt":"accepted","customer":"..."}`. A different worker must ship
its own handler and matching manifest. Packaging validates declarations and file
policy without importing or executing the handler.

```sh
export WEAVE_WHEEL_NAME="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/release/release.json"))["wheel"])')"
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main worker package \
  --manifest examples/worker/manifest.json --entrypoint examples/worker/main.py \
  --wheel "$WEAVE_WORK_DIR/release/artifacts/$WEAVE_WHEEL_NAME" \
  --requirements "$WEAVE_WORK_DIR/release/images/worker-requirements.txt" \
  --output "$WEAVE_WORK_DIR/worker-context" --format json
docker --context "$WEAVE_DOCKER_CONTEXT" build \
  --iidfile "$WEAVE_WORK_DIR/worker-image.id" "$WEAVE_WORK_DIR/worker-context"
export WEAVE_WORKER_IMAGE="$(cat "$WEAVE_WORK_DIR/worker-image.id")"
docker --context "$WEAVE_DOCKER_CONTEXT" image inspect \
  --format '{{.Id}} {{.Config.User}}' "$WEAVE_WORKER_IMAGE"
```

Expected: package JSON records the wheel and canonical manifest hashes; the image
uses nonroot UID/GID 65532. The context contains only the selected wheel, locked
requirements, entrypoint, manifest, generated build files and legal notices.
Input symlinks, archive escapes/duplicate entries, unlocked requirements, invalid
schemas and occupied outputs fail before a complete context is produced. A missing
`build.json` completion marker means a preparation attempt is incomplete.

## Provision authority and private worker configuration

The operator recipe below uses the existing **native administration service**. It
verifies real host and worker tokens, resolves the already-linked host identity,
and calls `AccessService.create_principal` and `link_identity` through the native
PyFly graph. It uses the nonowner application database identity and normal service
authorization. There is no raw SQL, fabricated identity, new route or automatic
image authority. Principal creation/linking currently has this explicit Python
administration boundary; public HTTP exposes tenant creation and grants.

In the first standalone terminal, with runtime and identity configuration loaded,
run once for this fresh runtime database:

```sh
"$WEAVE_PYTHON" examples/provision_worker.py --provider local-keycloak \
  --output "$WEAVE_WORK_DIR/worker-principal.json"
"$WEAVE_PYTHON" examples/admit_worker.py \
  --scope-receipt "$WEAVE_WORK_DIR/first-run.json" \
  --principal-receipt "$WEAVE_WORK_DIR/worker-principal.json" \
  --manifest "$WEAVE_WORK_DIR/worker-context/manifest.json" \
  --image "$WEAVE_WORKER_IMAGE" --output "$WEAVE_WORK_DIR/worker-release.json"
```

Expected: the first command reports a verified linked worker and writes a private
receipt. The second obtains a fresh host token, admits the exact release, grants
`worker` only for the current environment/release/task references, publishes the
matching action/workflow and activates it. All IDs come from actual responses.
The worker gets no authoring grant. Do not rerun identity linking against an
already-linked worker; failed attempts retain their partial state for inspection.

Start the example effect receiver in a third terminal using the same
`WEAVE_WORK_DIR` path:

```sh
python3 examples/idempotent_receiver.py --database "$WEAVE_WORK_DIR/effects.sqlite" \
  --host 0.0.0.0 --port 8090
```

Expected: `Local idempotent receiver ready`. Its independently durable SQLite
receipts survive receiver, API and worker restarts; a repeated operation key with
the same body returns its original receipt and a conflicting body is rejected.
This is a local demonstration target, not production receiver certification.
Restart the API terminal with `--host 0.0.0.0` when a container must reach it on
your owned development network; normal bearer authorization remains required.
Check readiness before proceeding:

```sh
"$WEAVE_PYTHON" - <<'PY'
import os, httpx
for url in ("http://127.0.0.1:8090/health", "http://127.0.0.1:" + os.environ["WEAVE_API_PORT"] + "/health/ready"):
    assert httpx.get(url, timeout=3, trust_env=False).status_code == 200
print("API and effect receiver are ready")
PY
export WEAVE_API_URL="http://host.docker.internal:$WEAVE_API_PORT"
export WEAVE_TOKEN_URL="http://host.docker.internal:$WEAVE_KEYCLOAK_PORT/realms/weave/protocol/openid-connect/token"
export WEAVE_EFFECT_URL=http://host.docker.internal:8090/effect
export WEAVE_ENVIRONMENT_URL="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/worker-release.json"))["environment_url"])')"
export WEAVE_WORKER_RELEASE_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["WEAVE_WORK_DIR"]+"/worker-release.json"))["release_id"])')"
```

`host.docker.internal` must resolve from your selected Docker runtime. Use its
host-gateway mapping or an explicit reachable owned host address on Linux. The
configured Keycloak issuer stays unchanged when its token endpoint is reached
through a host gateway. Outside localhost development, use trusted HTTPS origins.

The example needs these environment values in a **new owner-only** file:

| Variable | Required value |
| --- | --- |
| `WEAVE_API_URL` | API origin reachable from the container |
| `WEAVE_TOKEN_URL` | Trusted machine-token endpoint reachable from the container |
| `WEAVE_WORKER_SECRET` | Provisioned worker client credential |
| `WEAVE_ENVIRONMENT_URL` | Environment API path assembled from actual scope IDs |
| `WEAVE_WORKER_RELEASE_ID` | ID returned by release admission |
| `WEAVE_EFFECT_URL` | Owned idempotent receiver endpoint |

The worker receives no database URL, PostgreSQL credential, migration credential
or identity administrator secret. After setting these six values in your operator
shell, create the file without printing or copying any unrelated environment:

```sh
python3 - <<'PY'
import os
from pathlib import Path
keys = ("WEAVE_API_URL", "WEAVE_TOKEN_URL", "WEAVE_WORKER_SECRET",
        "WEAVE_ENVIRONMENT_URL", "WEAVE_WORKER_RELEASE_ID", "WEAVE_EFFECT_URL")
values = {key: os.environ[key] for key in keys}
assert all(value and "\n" not in value and "\r" not in value for value in values.values())
path = Path(os.environ["WEAVE_WORK_DIR"]) / "worker.env"
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
    stream.write("".join(key + "=" + value + "\n" for key, value in values.items()))
print("Owner-only worker configuration created")
PY
```

`localhost` inside a container is that container. Configure reachable, trusted
origins explicitly. The example's localhost Keycloak trust settings and a remote
worker's container routing must describe the same issuer; do not change the issuer
claim to work around connectivity. Production identity uses HTTPS.

## Deploy only the verified worker image

```sh
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main worker deploy --target compose \
  --image "$WEAVE_WORKER_IMAGE" --project "$WEAVE_LAUNCH_ID-worker" \
  --context "$WEAVE_DOCKER_CONTEXT" --directory "$WEAVE_WORK_DIR/worker-context" \
  --env-file "$WEAVE_WORK_DIR/worker.env" --format json \
  > "$WEAVE_WORK_DIR/worker-deployment.json"
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
receipt = json.loads((Path(os.environ["WEAVE_WORK_DIR"]) / "worker-deployment.json").read_text())
subprocess.run(["docker", "--context", receipt["context"], "inspect", "--format",
                "{{.State.Status}} {{.Image}}", receipt["container_id"]], check=True)
PY
```

The command inspects the local context/image and verifies wheel and manifest
labels. It refuses an existing project. Creation uses a unique ownership nonce and
`--no-recreate`; it starts only the exact newly created container whose labels and
image were verified. A competing foreign container is not started, replaced or
stopped. Subprocess output and runtime are bounded. No image is pulled or built by
the deploy command, and secret file contents do not appear in its result. Raw env-file mode preserves
literal dollar signs and quotes in credentials.

Expected: the returned receipt identifies the exact container/image and a
`stop_argv` array. Container `running` is process liveness, not release readiness:
confirm the instance was admitted through the public workers API and submit a
workflow pinned to the release. A registration or token error exits safely; inspect
only protected logs. The ordinary worker contains no crash-test switch.

Run the activated worker workflow and wait for its actual output:

```sh
"$WEAVE_PYTHON" - <<'PY'
import asyncio, json, os
from pathlib import Path
from uuid import uuid4
import httpx
root = Path(os.environ["WEAVE_WORK_DIR"])
receipt = json.loads((root / "worker-release.json").read_text())
async def main():
    async with httpx.AsyncClient(timeout=10, trust_env=False) as identity:
        response = await identity.post(os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave/protocol/openid-connect/token",
            data={"grant_type": "client_credentials"}, auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]))
        response.raise_for_status()
        token = response.json()["access_token"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:" + os.environ["WEAVE_API_PORT"],
            timeout=10, trust_env=False, headers={"Authorization": "Bearer " + token}) as client:
        response = await client.post(receipt["environment_url"] + "/runs",
            json={"activation_id": receipt["activation_id"], "input": {"customer": "demo"}},
            headers={"Idempotency-Key": str(uuid4())})
        response.raise_for_status()
        run_id = response.json()["id"]
        async with asyncio.timeout(60):
            while True:
                response = await client.get(receipt["environment_url"] + "/runs/" + run_id)
                response.raise_for_status()
                run = response.json()
                if run["state"]["status"] == "succeeded":
                    assert run["state"]["output"] == {"receipt": "accepted", "customer": "demo"}
                    print(json.dumps({"run_id": run_id, "status": "succeeded", "output": run["state"]["output"]}))
                    break
                await asyncio.sleep(0.25)
asyncio.run(main())
PY
```

Expected: the actual run ID, `succeeded`, and the accepted customer receipt. A
failure or timeout is not readiness; inspect the public run/incident state and
protected worker logs. Do not manufacture a completion or clear an incident to
make the walkthrough pass.

The example stops claims before its startup token expires and drains inside its
finite lifetime. A supervisor may start a new invocation after it exits; this is
not transparent token refresh. Its configured task budget is 180 seconds with a
30-second credential margin. See the worker protocol before adapting that budget.

## Configure and run the API and native executor containers

Keep the independent receiver on port 8090 running. The next recipe admits a
native read-only HTTP capability, grants the already verified worker principal
only that release/task, pins its connection and activates a workflow. It reads the
receiver's `/health` endpoint and performs no provider message action.

```sh
export WEAVE_API_URL="http://127.0.0.1:$WEAVE_API_PORT"
"$WEAVE_PYTHON" examples/admit_native.py \
  --scope-receipt "$WEAVE_WORK_DIR/first-run.json" \
  --principal-receipt "$WEAVE_WORK_DIR/worker-principal.json" \
  --manifest examples/host_product/http-manifest.json \
  --image "$WEAVE_SERVER_IMAGE" --output "$WEAVE_WORK_DIR/native-release.json"
export WEAVE_API_ENV_FILE="$WEAVE_WORK_DIR/api-container.env"
export WEAVE_NATIVE_ENV_FILE="$WEAVE_WORK_DIR/native-container.env"
export WEAVE_WORKER_ENV_FILE="$WEAVE_WORK_DIR/worker.env"
"$WEAVE_PYTHON" - <<'PY'
import json, os, shlex
from pathlib import Path
from sqlalchemy import make_url
root = Path(os.environ["WEAVE_WORK_DIR"])
runtime = dict(line.split("=", 1) for line in (root / "runtime.env").read_text().splitlines())
runtime = {key: shlex.split(value)[0] for key, value in runtime.items()}
providers = json.loads(runtime["WEAVE_OIDC_PROVIDERS"])
for provider in providers:
    provider["jwks_uri"] = "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/certs"
def database(key):
    return make_url(runtime[key]).set(host="postgres", port=5432).render_as_string(hide_password=False)
api = {"WEAVE_DATABASE_URL": database("WEAVE_DATABASE_URL"),
       "WEAVE_SCHEDULER_DATABASE_URL": database("WEAVE_SCHEDULER_DATABASE_URL"),
       "WEAVE_OIDC_PROVIDERS": json.dumps(providers, separators=(",", ":"))}
receipt = json.loads((root / "native-release.json").read_text())
native = {"WEAVE_DATABASE_URL": database("WEAVE_DATABASE_URL"),
          "WEAVE_SCHEDULER_DATABASE_URL": database("WEAVE_SCHEDULER_DATABASE_URL"),
          "WEAVE_SCHEDULER_ENABLED": "false", "WEAVE_NATIVE_IMAGE_DIGEST": os.environ["WEAVE_SERVER_IMAGE"],
          "WEAVE_NATIVE_EXECUTORS": json.dumps([receipt["executor"]], separators=(",", ":")),
          "WEAVE_HTTP_PRIVATE_NETWORKS": '["0.0.0.0/0"]'}
for name, values in (("WEAVE_API_ENV_FILE", api), ("WEAVE_NATIVE_ENV_FILE", native)):
    with os.fdopen(os.open(os.environ[name], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write("\n".join(key + "=" + value for key, value in values.items()) + "\n")
print("Private nonowner API and native executor configuration written")
PY

docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml -f compose.runtime.yaml \
  -f compose.local-runtime.yaml config --quiet
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml -f compose.runtime.yaml \
  -f compose.local-runtime.yaml up --detach --no-recreate --no-build --pull never api native-executor
```

The localhost identity fixture binds Keycloak's namespace to the API container.
`compose.local-runtime.yaml` uses `network_mode: service:keycloak`, so JWKS remains
on trusted HTTP loopback port 8080 **inside that namespace**; the token issuer
remains the configured `WEAVE_KEYCLOAK_TEST_URL` followed by `/realms/weave`. The
standalone setup reserved `WEAVE_CONTAINER_API_PORT` for this container API's port
8000. It is a second API replica alongside the foreground API on `WEAVE_API_PORT`. The normal runtime Compose
file supports separately reachable HTTPS identity endpoints without that local
override. This local API shares Keycloak's network lifecycle: stop the API before
replacing its Keycloak container, and launch a new API against the selected new
namespace afterward. Keeping an API attached to an old namespace is not a valid
upgrade procedure. Do not weaken trusted endpoint validation or change an issuer claim to
work around container routing.

The local native executor has an explicit private-network egress allowance for
the owned demonstration receiver. Narrow that policy and configure trusted HTTPS
destinations for deployment outside this local exercise. It receives no migration
credential or Docker socket. Its authority comes from the configured local worker
principal plus current grants and immutable release/connection pins.

Verify actual readiness and run the native workflow through the container API:

```sh
"$WEAVE_PYTHON" - <<'PY'
import asyncio, json, os
from pathlib import Path
from uuid import uuid4
import httpx
async def main():
    base = "http://127.0.0.1:" + os.environ.get("WEAVE_CONTAINER_API_PORT", "8081")
    receipt = json.loads((Path(os.environ["WEAVE_WORK_DIR"]) / "native-release.json").read_text())
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        async with asyncio.timeout(60):
            while True:
                try:
                    response = await client.get(base + "/health/ready")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.5)
        response = await client.post(os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave/protocol/openid-connect/token",
            data={"grant_type": "client_credentials"}, auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]))
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        response = await client.post(base + receipt["environment_url"] + "/runs",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"activation_id": receipt["activation_id"], "input": {}})
        response.raise_for_status()
        run_id = response.json()["id"]
        async with asyncio.timeout(60):
            while True:
                response = await client.get(base + receipt["environment_url"] + "/runs/" + run_id, headers=headers)
                response.raise_for_status()
                run = response.json()
                if run["state"]["status"] == "succeeded":
                    assert run["state"]["output"] == {"status": 200, "body": {"ready": True}}
                    print(json.dumps({"run_id": run_id, "status": "succeeded", "output": run["state"]["output"]}))
                    break
                await asyncio.sleep(.25)
asyncio.run(main())
PY
```

Expected: the actual run ID and successful native read output. Startup never
migrates automatically. Apply packaged migrations with the separate migration
identity before selecting a new runtime artifact. The API image and native image
share exact package bytes but have separate runtime configurations and authority.

## Stop the intended scope

To stop **only the deployed worker**, execute the exact argv from its receipt:

```sh
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
receipt = json.loads((Path(os.environ["WEAVE_WORK_DIR"]) / "worker-deployment.json").read_text())
subprocess.run(receipt["stop_argv"], check=True)
PY
```

The 30-second container stop grace is a hard outer bound. A task may outlive that
grace; lease fencing, idempotency and incident reconciliation govern recovery.
Stopping a process does not undo a request already accepted by an external system.

For **all runtime containers in this installation**, use the explicitly selected
runtime Compose project:

```sh
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml -f compose.runtime.yaml \
  -f compose.local-runtime.yaml stop --timeout 30 api native-executor
```

Stop PostgreSQL/Keycloak separately using the standalone guide only after every
writer is fenced. Keep volumes and secret files. Use the
[backup/restore procedure](backup-restore.md) before maintenance that requires a
restorable copy.
