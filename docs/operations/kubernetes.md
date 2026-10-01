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

# Deploy Weave on an existing Kubernetes cluster

This recipe deploys an exact server image, performs an explicit migration, and
verifies a public workflow on an operator-provisioned cluster. Use the
[cloud deployment overview](cloud-deployment.md) to select AWS EKS, Azure AKS or
Google GKE, registry access and cluster credentials. The same manifests apply
after those provider-specific prerequisites are ready.

This is a deployment starting point, not evidence that every managed PostgreSQL
service accepts Weave's migration ownership model. **Rehearse the selected
artifact's complete migrations on the intended database service before choosing
it for production.** The database preflight below is an acceptance gate.

![Cloud infrastructure, explicit migration and runtime deployment boundaries](../diagrams/cloud-deployment.svg)

Read the cloud topology from prepared infrastructure to the explicit migration
and runtime stages. The API owns scheduling in this example. The remote worker
talks to the API; the migration owner belongs only to the Job and bootstrap operator.

[Open diagram at full size](../diagrams/cloud-deployment.svg)

## 1. Prepare the owned environment

Run commands from the repository root. Select an existing authorized context and
namespace; do not rely on whichever cluster is currently selected:

```sh
# Check required selections and create a private directory for this deployment's manifests and receipts.
: "${WEAVE_KUBE_CONTEXT:?Set the context from your provider chapter}"
: "${WEAVE_KUBE_NAMESPACE:?Set your existing authorized namespace}"
export WEAVE_CLOUD_DIR="$HOME/weave-cloud-$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')"
umask 077
mkdir "$WEAVE_CLOUD_DIR"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" get deployments
```

Use the context and namespace selected in your provider chapter. If you arrived
directly, set both to the existing authorized names before running the block.
Retain this private directory and the selected names. You also need:

| Prerequisite | Acceptance evidence before proceeding |
| --- | --- |
| Registry image | Exact published server image reference `registry/repository@sha256:…`, built from the prepared wheel and correct dependency closure; nodes can pull it |
| PostgreSQL | Dedicated database, tested driver/TLS configuration, explicit migration owner and resulting nonowner runtime identities |
| HTTPS OIDC/CIAM provider | Registered clients; expected issuer, JWKS URI, audience, subjects, and access-token claims verified using [identity setup](identity-and-secrets.md#use-your-own-identity-provider) |
| Network | API/Job can reach the database; API can reach HTTPS JWKS; workers can reach API, token endpoint and intended effect receiver |
| Operator environment | Exact installed `server`/`client`-capable Python named by `WEAVE_PYTHON`, with database reachability for deliberate bootstrap |
| Recovery | Environment-specific restorable backup and independent external-effect receipts |

Configure registry pull access using your cluster's identity integration or a
namespace pull secret before applying manifests. The samples do not grant cloud
IAM permissions. They also do not provision an identity provider, a database, ingress, a secret
operator, network policies or a telemetry collector. Choose those deliberately;
allow only the traffic in the prerequisite table.

## 2. Prove the database authority model

Do **not** run `scripts/setup-runtime.py` against a managed database. Its guard
accepts only the local control fixture and it provisions a fresh local database.
Production provisioning belongs to the database administrator.

The selected migration identity must own the new database/schema objects and be
able to execute the packaged role creation, policy creation, grants, and
`ALTER FUNCTION … OWNER TO …` operations. Merely having permission to create tables
is insufficient. The migrations create application/scheduler group roles and
narrow owners such as `weave_catalog_reader`, `weave_trigger_reader` and
`weave_retention_owner`. In revision `0021_operations`, an existing retention owner
must have its exact migration-defined comment, no login or elevated flags,
`NOINHERIT`, and no memberships granting that role. Precreating roles with
convenient broad memberships can therefore fail the migration.

Managed PostgreSQL administrators are not universally PostgreSQL superusers.
Have the DBA rehearse the unmodified packaged migrations on a new disposable
**owned rehearsal database**, using the same provider/version/privileges intended
for deployment. Keep its result and inspect role/function ownership. This guide
does not supply provider-independent privileged bootstrap SQL: that would assume
permissions which the managed service may not allow. If this rehearsal fails,
stop here and resolve the provider's ownership contract; do not edit migration
markers, remove RLS or add runtime owner privileges.

After the successful migration in step 4 creates the packaged roles, a DBA may
create separate runtime logins. The following is a **new-login example for psql**,
not an idempotent production provisioning script. Choose unique names, run it
only in the selected database, and set passwords with protected interactive
prompts or your secret provisioning system:

```sql
-- Create separate application and scheduler logins; neither receives owner or superuser privileges.
CREATE ROLE weave_api_login LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT weave_app TO weave_api_login;
CREATE ROLE weave_catalog_login LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT weave_scheduler TO weave_catalog_login;
\password weave_api_login
\password weave_catalog_login
```

The migration creates the table/function grants; do not duplicate them with
`GRANT ALL`. The scheduler login must have no `weave_app`, catalog-owner or
retention-owner membership and no direct business-table/column grants. It needs
schema usage, the packaged scheduler/catalog function execution grants and schema
marker reads. If the DBA removed default database `CONNECT`, grant connection to
these two logins explicitly on the selected database.

Build application and scheduler `postgresql+asyncpg` URLs with identical driver,
host spelling, port, database and query options, differing only in credentials.
Use the provider's tested TLS/certificate configuration with the packaged asyncpg
driver. The samples assume necessary public trust is already in the image; mount
any extra required CA material and adapt the URLs before rehearsal. The application
login must not own public tables or have superuser/BYPASSRLS authority. Startup
validates these boundaries and always requires catalog compatibility authority.

Sources: [packaged migration entry point](../../src/firefly_weave/persistence/migrations.py),
[operations migration](../../migrations/versions/0021_operations.py), and
[catalog authority checks](../../src/firefly_weave/operations/compatibility.py).

## 3. Render the exact manifests

Set `WEAVE_SERVER_IMAGE_REF` to the actual registry reference from your completed
push. A Docker engine image ID is not the registry manifest digest used in this
reference. Kubernetes supports digest-pinned images; retain the build record and
registry digest together. See [Kubernetes image names](https://kubernetes.io/docs/concepts/containers/images/).

```sh
# Fill the templates with the exact server image digest and a unique migration Job name.
export WEAVE_MIGRATION_JOB="weave-migrate-$(python3 -c 'from uuid import uuid4; print(uuid4().hex[:12])')"
python3 - <<'PY'
import os, re
from pathlib import Path
image = os.environ["WEAVE_SERVER_IMAGE_REF"]
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", image), "Use the real registry digest reference"
job = os.environ["WEAVE_MIGRATION_JOB"]
assert re.fullmatch(r"weave-migrate-[a-z0-9-]{1,40}", job)
root = Path(os.environ["WEAVE_CLOUD_DIR"])
for name in ("api.yaml", "migrate.yaml"):
    text = (Path("deploy/kubernetes") / name).read_text()
    text = text.replace("__WEAVE_SERVER_IMAGE__", image).replace("__WEAVE_MIGRATION_JOB__", job)
    with (root / name).open("x") as stream:
        stream.write(text)
print("Rendered API and unique migration Job manifests")
PY
```

Review the files and the [template inventory](../../deploy/kubernetes/README.md).
[api.yaml](../../deploy/kubernetes/api.yaml) selects one API
replica, scheduling enabled, `Recreate`, a private ClusterIP service, nonroot
UID/GID 65532, a read-only filesystem and a bounded writable `/tmp`. Resource
requests/limits are initial examples to measure against your workloads, not
capacity guarantees. The startup probe uses `/health/live`; readiness uses
`/health/ready`. No liveness restart policy hides a persistent compatibility
failure. A failed readiness probe removes the pod from Service endpoints, which
also affects normal inspection through that Service. See
[Kubernetes probe behavior](https://kubernetes.io/docs/concepts/workloads/pods/probes/).

`Recreate` is a workload rollout strategy, not a database fence. Before an upgrade,
stop all other writers and follow [upgrade acceptance](upgrades.md); never allow
mixed schema versions to write during migration. See
[Kubernetes Deployment strategies](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#strategy).

## 4. Supply only migration authority and run the Job

Have your secret system materialize a new mode-0600 raw file at
`$WEAVE_CLOUD_DIR/migration.env` with exactly these keys:

| Key | Value |
| --- | --- |
| `WEAVE_MIGRATION_DATABASE_URL` | Owner URL for the selected target database |
| `WEAVE_OPERATIONS_POLICY` | Exact intended JSON policy; `{}` keeps defaults |

Use raw `KEY=value` lines, with JSON on one line, no `export`, no shell quotes and
no multiline values. Do not source this raw file as a shell script. The Kubernetes
Secret is a deployment credential object: restrict its RBAC access and configure
your cluster's at-rest encryption/secret lifecycle. The Job references only the
two named keys. [Kubernetes documents Secret environment injection](https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/).

```sh
# Provide only migration credentials to the Job, validate its manifest, then wait for completion.
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" create secret generic weave-migration \
  --from-env-file="$WEAVE_CLOUD_DIR/migration.env"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply \
  --dry-run=server -f "$WEAVE_CLOUD_DIR/migrate.yaml"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply -f "$WEAVE_CLOUD_DIR/migrate.yaml"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" wait \
  --for=condition=complete --timeout=900s "job/$WEAVE_MIGRATION_JOB"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" logs "job/$WEAVE_MIGRATION_JOB"
```

Expected: completed Job and `Weave schema is current`. Its selected image runs
`python -I -m firefly_weave.cli.main admin migrate`. A timeout or failed Job is
not success: inspect protected events/logs and keep the retained attempt. The Job
has no automatic retry and no automatic deletion. Choose a new unique Job name
for a deliberate retry after diagnosis. Secret creation refuses an occupied name;
use your existing credential lifecycle instead of blindly replacing it.

Now have the DBA create and verify the runtime logins from step 2. Complete
identity bootstrap using the same selected artifact from a protected operator
environment with the owner URL supplied by your secret system. Set
`WEAVE_PROVIDER_ID`, exact HTTPS `WEAVE_ISSUER`, and `WEAVE_HOST_SUBJECT` from trusted
provider administration or a verified host access token:

```sh
# Link the verified identity once; the receipt records the real local principal for subsequent grants.
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main admin bootstrap \
  --provider "$WEAVE_PROVIDER_ID" --issuer "$WEAVE_ISSUER" \
  --subject "$WEAVE_HOST_SUBJECT" --kind application \
  --output "$WEAVE_CLOUD_DIR/bootstrap.json"
```

The subject is the verified access token's `sub` value, not a guessed client ID
or email. In the local Keycloak example, this is a service-account UUID.
This creates a local administrator link; it does not issue a token or grant
business scope. Retain the private receipt and remove migration credentials from
the operator environment when this stage is done. Existing installations reuse
their identity links instead of repeating bootstrap.

## 5. Start the API with runtime credentials

Materialize a separate mode-0600 raw file `$WEAVE_CLOUD_DIR/api.env`:

| Key | Value |
| --- | --- |
| `WEAVE_DATABASE_URL` | Verified nonowner application URL |
| `WEAVE_SCHEDULER_DATABASE_URL` | Separate execute-only catalog URL for the same database |
| `WEAVE_OIDC_PROVIDERS` | JSON array with your exact trusted HTTPS provider configuration |
| `WEAVE_OPERATIONS_POLICY` | Same JSON policy used by the migration Job |

For the OIDC array, replace these illustrative values with your provider
registration. This example assumes an RS256 access token with `client_id` and
`token_use=access` claims; use the actual verified claim contract of your provider:

```json
[{
  "provider_id": "organization-ciam",
  "issuer": "https://identity.example/",
  "jwks_uri": "https://identity.example/.well-known/jwks.json",
  "audience": "weave-api",
  "clients": {"weave-host": "application", "weave-worker": "application", "weave-cli": "human"},
  "algorithms": ["RS256"],
  "client_claim": "client_id",
  "token_class_claim": "token_use",
  "token_class_value": "access",
  "local_development": false
}]
```

`identity.example` is a placeholder, not an available provider. Match provider ID
to bootstrap, and verify the configured audience, client, subject, and token-purpose
claims. Follow [provider setup](identity-and-secrets.md#use-your-own-identity-provider)
before starting the API. Neither Kubernetes workload identity nor a cloud IAM
login automatically becomes a Weave principal. No identity-provider administrator
secret belongs in `api.env`.

```sh
# Start the API with runtime credentials and wait for Kubernetes to report a successful rollout.
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" create secret generic weave-api-runtime \
  --from-env-file="$WEAVE_CLOUD_DIR/api.env"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply \
  --dry-run=server -f "$WEAVE_CLOUD_DIR/api.yaml"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply -f "$WEAVE_CLOUD_DIR/api.yaml"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" rollout status deployment/weave-api --timeout=300s
```

For an operator-only first run, open another terminal and restore these same
context/namespace values, then keep this loopback tunnel running:

```sh
# Keep this terminal open: it forwards a loopback port to the API for initial verification.
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" port-forward \
  --address 127.0.0.1 service/weave-api 8080:8000
```

Choose a free local port if 8080 is occupied; set `WEAVE_API_URL` in the operator
terminal to the matching `http://127.0.0.1:PORT`. This tunnel is not your production
endpoint. Configure an operator-selected ingress/Gateway with trusted HTTPS,
DNS, certificate renewal and appropriate network controls before serving remote
clients. Route it to `weave-api:8000`; controller annotations and TLS setup depend
on your selected provider/controller, so this repository does not invent a
universal ingress manifest. Set `WEAVE_API_URL` to that HTTPS origin for the
remote verification.

## 6. Verify identity and one public workflow

In the operator environment, load the API's exact OIDC JSON and host credential
from your protected secret system. The following obtains and verifies a fresh
Keycloak host token without printing it; it assumes the `weave-host` client and
token path used above. Save this refresh helper in the private directory. It checks
API readiness before token acquisition, verifies the intended subject, and
atomically creates or refreshes the owned token receipt:

```sh
# Check API readiness, refresh the host token privately, then create and verify the first workflow run.
cat > "$WEAVE_CLOUD_DIR/refresh-token.py" <<'PYTOKEN'
import asyncio, json, os, stat, tempfile
from pathlib import Path
import httpx
from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig
async def main():
    profiles = [ProviderConfig.model_validate(p) for p in json.loads(os.environ["WEAVE_OIDC_PROVIDERS"])]
    config, = [p for p in profiles if p.provider_id == os.environ["WEAVE_PROVIDER_ID"]]
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        ready = await client.get(os.environ["WEAVE_API_URL"].rstrip("/") + "/health/ready")
        if ready.status_code != 200:
            raise SystemExit("Selected API is not ready; inspect its compatibility report")
        response = await client.post(config.issuer + "/protocol/openid-connect/token",
            data={"grant_type":"client_credentials"}, auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]))
        if response.status_code != 200:
            raise SystemExit("Host token acquisition failed; inspect the protected provider configuration")
        token = response.json()["access_token"]
    identity = await OIDCVerifier(config).verify(token)
    assert identity.client_id == "weave-host" and identity.subject == os.environ["WEAVE_HOST_SUBJECT"]
    root = Path(os.environ["WEAVE_CLOUD_DIR"])
    destination = root / "host-token.json"
    try:
        prior = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        assert stat.S_ISREG(prior.st_mode) and prior.st_uid == os.getuid() and prior.st_mode & 0o077 == 0
        assert json.loads(destination.read_text())["subject"] == identity.subject
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
        json.dump({"access_token": token, "subject": identity.subject}, stream)
        temporary = Path(stream.name)
    temporary.replace(destination)
    print("API ready; verified host token saved privately")
asyncio.run(main())
PYTOKEN
"$WEAVE_PYTHON" "$WEAVE_CLOUD_DIR/refresh-token.py" &&
"$WEAVE_PYTHON" examples/first_run.py --api-url "$WEAVE_API_URL" \
  --token-file "$WEAVE_CLOUD_DIR/host-token.json" \
  --bootstrap-receipt "$WEAVE_CLOUD_DIR/bootstrap.json" \
  --output "$WEAVE_CLOUD_DIR/first-run.json"
```

Expected: `API ready; verified host token saved privately`, then a real run ID,
`succeeded`, and `{"message":"Hello, Weave"}`. The `&&` prevents first-run provisioning
if refresh fails. For later token renewal, rerun only `refresh-token.py`; it refuses
to replace a receipt for a different subject. Do not rerun `first_run.py` just to
refresh authentication. The first-run helper
creates a new tenant/project/environment and explicit grants, publishes a workflow,
activates it and reads its durable outcome. Its environment name is `local`, even
when the server is on Kubernetes; it is an example resource name, not a deployment
attestation. Use it once for this new demonstration scope. Keep its IDs; follow
[CLI operations](../reference/cli.md#remote-authoring-and-operations) for subsequent
operations instead of repeating provisioning.

Continue with the [cloud CLI handoff](../guides/cli-tutorial.md#use-these-commands-with-a-cloud-installation) to inspect this
scope, publish changes, compare versions and operate runs from the terminal.

A successful constant-output run verifies the API path. It does not exercise a
remote worker, native connector, real provider delivery or your restore procedure.

## 7. Add a remote worker only when required

The optional [worker manifest](../../deploy/kubernetes/worker.yaml) starts with
`replicas: 0`. Package/build/push your worker with its own locked closure as in
[deployment](deployment.md#prepare-a-worker-context), then use its exact registry
image reference in the template. Keep the canonical manifest, wheel hash and
published image identity together. Release admission still binds the implementation
to an explicit `sha256:` identity; an image pull is not release admission or
runtime attestation. Establish how your admitted identity maps to the pushed
artifact and retain that build evidence.

For the included `example-record@1.0.0` worker, first provision its separate worker
principal, admit/grant the exact release/task in the intended scope, and activate
the workflow. Load the cloud runtime's `WEAVE_DATABASE_URL`,
`WEAVE_SCHEDULER_DATABASE_URL`, and `WEAVE_OIDC_PROVIDERS` into the protected
operator environment; do not source the local exercise's `runtime.env`.
Select the real `WEAVE_PROVIDER_ID` used in step 6 and set
`WEAVE_KEYCLOAK_TEST_URL` to your HTTPS Keycloak base URL for `admit_worker.py`
(the variable name is historical; that helper appends `/realms/weave`). It therefore
requires the `weave` realm and the `weave-host` and `weave-worker` clients.
Keep `WEAVE_API_URL` set to the cloud API origin reachable from the operator.
Load the corresponding `WEAVE_HOST_SECRET` and `WEAVE_WORKER_SECRET` privately
from your secret system. Identity linking uses the nonowner application DB
connection and normal administrator authorization; none of this operator
database configuration is copied into the worker.

Keep `WEAVE_WORK_DIR` pointing to the local build receipts from the cloud
overview. Use `WEAVE_CLOUD_DIR` for the new cloud scope and administration
receipts. Run this once for an unlinked worker identity in that cloud database:

```sh
# Create the worker identity and admit its exact build for the environment returned by the first run.
"$WEAVE_PYTHON" examples/provision_worker.py --provider "$WEAVE_PROVIDER_ID" \
  --output "$WEAVE_CLOUD_DIR/worker-principal.json" &&
"$WEAVE_PYTHON" examples/admit_worker.py \
  --scope-receipt "$WEAVE_CLOUD_DIR/first-run.json" \
  --principal-receipt "$WEAVE_CLOUD_DIR/worker-principal.json" \
  --manifest "$WEAVE_WORK_DIR/worker-context/manifest.json" \
  --image "$(cat "$WEAVE_WORK_DIR/cloud-worker-image.id")" \
  --output "$WEAVE_CLOUD_DIR/worker-release.json"
```

Expected: `Verified worker identity linked; private receipt written`, then
`Release admitted, worker grant assigned, workflow activated`. The image identity
comes from the final cloud-platform build, while all resource IDs come from the
cloud receipts. The `&&` stops admission if identity linking fails. Both helpers
retain partial state on failure; inspect it before continuing and do not repeat
identity linking against an already-linked identity.

Provide an owned receiver implementing the example's durable `Idempotency-Key`
contract and expected receipt body. The unauthenticated local demonstration
receiver is not a production internet service. Set the three URLs below for
reachability from the worker pod, preserving the same cloud API and identity
provider. Keep `WEAVE_WORKER_SECRET` loaded privately. The two Weave identifiers
will be read directly from the new admission receipt:

| Key | Source |
| --- | --- |
| `WEAVE_API_URL` | API origin reachable from the worker pod |
| `WEAVE_TOKEN_URL` | Trusted HTTPS Keycloak token endpoint |
| `WEAVE_WORKER_SECRET` | Worker client credential from the secret system |
| `WEAVE_ENVIRONMENT_URL` | Real environment API path from admission receipt |
| `WEAVE_WORKER_RELEASE_ID` | Actual admitted release UUID |
| `WEAVE_EFFECT_URL` | Owned reachable effect receiver endpoint |

Create the new owner-only file without printing the credential:

```sh
# Write only the worker's required settings to an owner-readable file; no database login is needed.
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["WEAVE_CLOUD_DIR"])
receipt = json.loads((root / "worker-release.json").read_text())
values = {key: os.environ[key] for key in (
    "WEAVE_API_URL", "WEAVE_TOKEN_URL", "WEAVE_WORKER_SECRET", "WEAVE_EFFECT_URL"
)}
values["WEAVE_ENVIRONMENT_URL"] = receipt["environment_url"]
values["WEAVE_WORKER_RELEASE_ID"] = receipt["release_id"]
assert all(value and "\n" not in value and "\r" not in value for value in values.values())
with os.fdopen(os.open(root / "worker.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
    stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
print("Private cloud worker configuration written")
PY
```

Set `WEAVE_WORKER_IMAGE_REF` to the worker's real registry digest reference, then:

```sh
# Pin the worker image and supply its private API credentials before applying the Deployment.
python3 - <<'PY'
import os, re
from pathlib import Path
image = os.environ["WEAVE_WORKER_IMAGE_REF"]
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", image)
text = Path("deploy/kubernetes/worker.yaml").read_text().replace("__WEAVE_WORKER_IMAGE__", image)
with (Path(os.environ["WEAVE_CLOUD_DIR"]) / "worker.yaml").open("x") as stream:
    stream.write(text)
PY
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" create secret generic weave-worker-runtime \
  --from-env-file="$WEAVE_CLOUD_DIR/worker.env"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply \
  --dry-run=server -f "$WEAVE_CLOUD_DIR/worker.yaml"
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" apply -f "$WEAVE_CLOUD_DIR/worker.yaml"
```

After confirming the release, grants, activation and receiver, start it deliberately:

```sh
# Start one admitted worker after configuration and identity checks have passed.
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" scale deployment/weave-worker --replicas=1
```

Verify an actual task workflow and independent receiver receipt using the public
run API. This worker has no HTTP health endpoint; a Kubernetes running pod is not
a readiness claim. The example invocation drains before token expiry and exits;
Kubernetes restarts a new invocation, which obtains a new token and instance. It
does not refresh a running invocation's token transparently. The 240-second
termination grace allows its declared 180-second task budget plus credential
margin, but lease fencing and reconciliation still govern interrupted effects.
A native executor would need a separate server deployment with application and
catalog credentials, admitted native release and connector policy; this remote
worker manifest cannot substitute for that authority.

## 8. Maintain the deployment deliberately

Retain the exact manifest/image/Job and first-run evidence. For a configuration
rotation, coordinate provider/database changes, replace the appropriate Secret
through your authorized lifecycle, and replace the consuming pods. An environment
variable sourced from a Secret is not updated inside an already-running process.
Use the [configuration ownership rules](configuration.md) to choose that process.

For an upgrade, stop ingress producers and all workers/dispatchers first, then
scale the API down and confirm the old pods and database writer sessions have
stopped. A Deployment strategy or a zero desired replica count alone is not proof
that no writer remains. Back up and rehearse the selected artifact; create a new
migration Job only after fencing. Reenable the API, require complete compatibility
and verify retained history plus a new authorized run before resuming writers.
Use [upgrades](upgrades.md), [observability](observability.md) and
[troubleshooting](troubleshooting.md). The guarded local restore helper does not
restore RDS, Azure Database for PostgreSQL or Cloud SQL; use your tested provider
recovery procedure with the same one-active-deployment and receipt-preservation
requirements.

These manifests are statically reviewable examples. Kubernetes admission, cloud
IAM, managed-database migration compatibility, TLS, load behavior and restore
acceptance must be verified in the environment you operate.
