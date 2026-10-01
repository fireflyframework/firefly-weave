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

# Deploy Weave on AWS, Azure, or Google Cloud

The local tutorial teaches the lifecycle. Cloud deployment moves the **same
application artifacts** into an operator-controlled network and process manager.
This guide uses Kubernetes as the shared application layer: EKS on AWS, AKS on
Azure, and GKE on Google Cloud. It is a deployment recipe for prepared infrastructure,
not a promise that the CLI creates a cloud account or a production platform.

Start with the [CLI tutorial](../guides/cli-tutorial.md) and
[local worker packaging](deployment.md). Understand publication, activation, and
worker admission before moving their processes to another environment.

![Build artifacts, registry, Kubernetes runtime, identity, and database](../diagrams/cloud-deployment.svg)

Read the top row as artifact delivery and the bottom row as runtime dependencies.
Pushing an image does not grant Weave worker authority. The API and workers must
still use valid identity links, scoped grants, and the admitted release.

## 1. Choose the infrastructure route

| Topic | Local tutorial | Cloud recipe |
| --- | --- | --- |
| Process manager | Compose and foreground Python | Kubernetes Deployments and explicit Jobs |
| Image location | Exact local Docker image ID | Private registry, pinned by registry digest |
| Durable state | Owned local PostgreSQL container | Qualified PostgreSQL installation with separate logins |
| Identity | Local Keycloak fixture | Operator-managed HTTPS Keycloak or another verified OIDC profile |
| Runtime secrets | Private local files | Process-specific Kubernetes Secrets supplied by your secret-management system |
| Network | Selected local ports and host gateway | Private database access, cluster Service, controlled HTTPS ingress/egress |
| Deployment command | `weave worker deploy --target compose` | Cloud CLI setup, then `kubectl` |

The provided Weave deployment command supports **local Compose only**. It neither
accepts `aws`/`azure`/`gcp` targets nor manages Kubernetes. Kubernetes manages the
containers; Weave manages their workflow and task authority.

Choose exactly one provider chapter, then return here:

- [AWS: EKS and ECR](aws.md).
- [Azure: AKS and ACR](azure.md).
- [Google Cloud: GKE and Artifact Registry](gcp.md).

These chapters identify the required provider resources, registry authentication,
cluster context, and image-pull permissions. Creating or keeping cloud resources
incurs provider charges; use your organization's approved infrastructure process.

## 2. Pass the infrastructure readiness gate

Do this before applying application manifests:

1. Select an owned Kubernetes cluster and namespace. Ensure its nodes can pull
   the registry images and reach the database, identity service, and intended
   integration destinations.
2. Rehearse the exact package's migrations against the chosen PostgreSQL service.
   Weave creates scoped roles and security-definer functions; a managed service's
   administrator is not necessarily an unrestricted PostgreSQL superuser. Follow
   the [database qualification procedure](kubernetes.md) with your DBA. Do not
   bypass a failed role/ownership check or treat a local migration as cloud evidence.
3. Prepare the migration owner, nonowner application login, and separate
   catalog/scheduler login. Keep them in separate secret bundles.
4. Configure an HTTPS identity issuer, JWKS endpoint, audience, client allowlist,
   and verified subject for bootstrap. Cloud IAM permission to pull an image is
   distinct from permission to call Weave.
5. Decide the API's HTTPS origin and route. The manifests expose an internal
   Service; they do not create public DNS, TLS certificates, or a provider load
   balancer. A loopback `kubectl port-forward` supports initial verification.
6. Set resource requests, limits, backup/restore, logging, and egress policy for
   your environment. The sample starts one API replica with scheduler ownership;
   capacity and availability testing must precede production scaling.

A Kubernetes Secret is a delivery mechanism for credentials, not a substitute for
cloud secret-store access controls, encryption, or rotation. This release does not
bundle automatic AWS Secrets Manager, Key Vault, or Secret Manager synchronization.
Your infrastructure must materialize the process-specific values.

## 3. Build images for the target architecture

Keep the prepared `release/images` and `worker-context` directories from
[local packaging](deployment.md). The provider chapter sets
`WEAVE_REGISTRY_PREFIX` and `WEAVE_KUBE_CONTEXT`. Keep your explicitly selected
local `WEAVE_DOCKER_CONTEXT`; the Kubernetes context is a separate selection.

Inspect the cluster's architecture before choosing a build platform:

```sh
# Read the node CPU architecture; the image must match the nodes that will run it.
kubectl --context "$WEAVE_KUBE_CONTEXT" get nodes \
  -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture
```

For an `amd64` node pool, set the platform shown below; use `linux/arm64` for an
`arm64` pool. Use matching node placement if a cluster has mixed architectures.
The command builds a single-platform image; it does not create a multiarch index.
A laptop's architecture is not evidence that its image will run on cloud nodes.

```sh
# Build API and example-worker images for that architecture and retain their local image IDs.
export WEAVE_TARGET_PLATFORM=linux/amd64
export WEAVE_IMAGE_TAG="weave-$(date -u +%Y%m%dT%H%M%SZ)"
docker --context "$WEAVE_DOCKER_CONTEXT" build \
  --platform "$WEAVE_TARGET_PLATFORM" --target server \
  --iidfile "$WEAVE_WORK_DIR/cloud-server-image.id" \
  "$WEAVE_WORK_DIR/release/images"
docker --context "$WEAVE_DOCKER_CONTEXT" build \
  --platform "$WEAVE_TARGET_PLATFORM" \
  --iidfile "$WEAVE_WORK_DIR/cloud-worker-image.id" \
  "$WEAVE_WORK_DIR/worker-context"
```

This combined example builds both the API and the remote worker so the push and
digest steps below have both inputs. An API-only rollout can use just the server
path, but must also omit every worker push/digest step. Building the
example worker does not implement an arbitrary business integration. Its manifest
and handler still require the exact effect-receiver contract explained in [the local worker exercise](deployment.md#prepare-a-worker-context).
Use `teams-server` or `kafka-server` instead of `server` when those complete
optional runtime dependency sets are needed. Preserve the package's build receipts.

## 4. Push and record registry identities

After completing the chosen provider's registry login, tag and push the two
images. Use the same explicit local Docker context that built them:

```sh
# Push both verified local images to the selected registry so cluster nodes can download them.
export WEAVE_SERVER_TAG="$WEAVE_REGISTRY_PREFIX/server:$WEAVE_IMAGE_TAG"
export WEAVE_WORKER_TAG="$WEAVE_REGISTRY_PREFIX/worker:$WEAVE_IMAGE_TAG"
docker --context "$WEAVE_DOCKER_CONTEXT" tag \
  "$(cat "$WEAVE_WORK_DIR/cloud-server-image.id")" "$WEAVE_SERVER_TAG"
docker --context "$WEAVE_DOCKER_CONTEXT" push "$WEAVE_SERVER_TAG"
docker --context "$WEAVE_DOCKER_CONTEXT" tag \
  "$(cat "$WEAVE_WORK_DIR/cloud-worker-image.id")" "$WEAVE_WORKER_TAG"
docker --context "$WEAVE_DOCKER_CONTEXT" push "$WEAVE_WORKER_TAG"
```

Continue only when each required push succeeds. Read the registry digest references
from the pushed images, keeping the selected repository unambiguous:

```sh
# Record immutable registry digests; Kubernetes uses these rather than mutable image tags.
docker --context "$WEAVE_DOCKER_CONTEXT" image inspect \
  --format '{{json .RepoDigests}}' "$WEAVE_SERVER_TAG" \
  > "$WEAVE_WORK_DIR/cloud-server-digests.json"
docker --context "$WEAVE_DOCKER_CONTEXT" image inspect \
  --format '{{json .RepoDigests}}' "$WEAVE_WORKER_TAG" \
  > "$WEAVE_WORK_DIR/cloud-worker-digests.json"
python3 - <<'PY'
import json, os, re, shlex
from pathlib import Path
root = Path(os.environ['WEAVE_WORK_DIR'])
lines = []
for kind in ('server', 'worker'):
    repository = os.environ['WEAVE_REGISTRY_PREFIX'] + '/' + kind
    candidates = json.loads((root / f'cloud-{kind}-digests.json').read_text())
    matches = [value for value in candidates if value.startswith(repository + '@sha256:')]
    assert len(matches) == 1, 'Inspect registry results before selecting an image'
    assert re.fullmatch(r'.+@sha256:[a-f0-9]{64}', matches[0])
    lines.append(f'export WEAVE_{kind.upper()}_IMAGE_REF={shlex.quote(matches[0])}')
with (root / 'cloud-images.env').open('x') as stream:
    stream.write('\n'.join(lines) + '\n')
print('Saved registry-pinned image references in cloud-images.env')
PY
source "$WEAVE_WORK_DIR/cloud-images.env"
```

If an image has no digest after pushing, inspect the provider registry and select
the actual `repository@sha256:...` reference; do not substitute a local image ID.
A repeated build should use a new tag and output receipt rather than overwrite
an existing release's identity files.

Two different SHA-256 identities matter:

| Identity | Used by | Obtain it from |
| --- | --- | --- |
| Registry manifest digest | Kubernetes `image:` for pulling immutable content | Pushed image / registry response |
| Image configuration ID | The admitted build identity used by the packaged worker/release contract | `cloud-worker-image.id` for that exact built image |

They usually differ. Record both. A registry push is neither release admission
nor runtime attestation. Admit the final target-platform build, not an earlier
laptop build with the same handler name.

## 5. Deploy the application and prove an execution

Continue with [the Kubernetes walkthrough](kubernetes.md) and its
[checked-in manifest templates](../../deploy/kubernetes/README.md). It covers:

1. Select the namespace and render pinned image references into a private directory.
2. Supply the separate runtime and migration secrets.
3. Run the explicit migration Job and verify its result.
4. Bootstrap a verified host identity and start the API.
5. Check readiness and execute a public API/CLI workflow.
6. Admit and authorize a remote-worker release before scaling its Deployment up.

Then reuse the [CLI workflow lifecycle](../guides/cli-tutorial.md), selecting the
cloud API origin, current token, and actual cloud scope IDs. Never reuse a local
tenant/activation/worker ID merely because the environment has the same name.

## 6. Operate the deployment

Use [observability](observability.md) for telemetry and durable evidence,
[backup and restore](backup-restore.md) for ownership/fencing principles, and
[upgrades](upgrades.md) for schema/artifact acceptance. The local restore helper
has a guarded local contract; it is not a managed-database restore automation.

An image rollout does not migrate a schema automatically. Conversely, reverting
an image does not undo a migration or external effect. Retain database recovery
points and test a compatible recovery path before changing a shared environment.
If a readiness, identity, or worker admission check fails, stop at that stage and
use its evidence rather than scaling additional replicas.

These manifests and commands are reference deployment material, checked against
Weave's code and Kubernetes object shapes. They have not been executed against
customer AWS, Azure, or GCP accounts. A production claim requires the migration,
identity, network, recovery, and real integration acceptance checks in that target.
