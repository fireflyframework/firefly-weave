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

# Prepare Google GKE and Artifact Registry

Use an **existing operator-provisioned GKE cluster** and an existing Docker-format
Artifact Registry repository. Install Google Cloud CLI, kubectl, Docker, Python 3,
and `gke-gcloud-auth-plugin`. Authenticate gcloud with your approved operator
identity. Cluster access and repository push access must already be granted.
This chapter configures clients without creating cloud infrastructure.

![Registry, Kubernetes, database, identity, and Weave deployment boundaries](../diagrams/cloud-deployment.svg)

Locate Artifact Registry at the image boundary and GKE at the runtime boundary.
The image-pull identity belongs to the cluster; your local login and Weave's
application identity have different jobs.
[Open the diagram at full size](../diagrams/cloud-deployment.svg).

## If you do not have infrastructure yet

Use the provider's [GKE cluster creation guide](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/creating-an-autopilot-cluster) to prepare an approved cluster and its network,
node capacity, and access controls. Provision the registry and intended namespace
through the same infrastructure process. Then return to step 1 with the actual
resource names. These resources incur charges; a provider quickstart is a learning
baseline, not a production availability or security design for Weave.

## 1. Select the project and actual resource locations

Replace placeholders with your operator's resource inventory. The cluster location
is a region for a regional cluster or a zone for a zonal cluster. The Artifact
Registry location can differ; obtain it from the repository record.

```sh
# Inspect the authenticated account, target project, existing cluster, and Docker repository.
export WEAVE_GCP_PROJECT='your-existing-project-id'
export WEAVE_GKE_CLUSTER='your-existing-cluster'
export WEAVE_GKE_LOCATION='your-cluster-region-or-zone'
export WEAVE_AR_LOCATION='your-repository-location'
export WEAVE_AR_REPOSITORY='your-existing-docker-repository'
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud projects describe "$WEAVE_GCP_PROJECT" --format='value(projectId)'
gcloud container clusters describe "$WEAVE_GKE_CLUSTER" \
  --project "$WEAVE_GCP_PROJECT" --location "$WEAVE_GKE_LOCATION" \
  --format='yaml(name,location,status)'
gcloud artifacts repositories describe "$WEAVE_AR_REPOSITORY" \
  --project "$WEAVE_GCP_PROJECT" --location "$WEAVE_AR_LOCATION" \
  --format='yaml(name,format)'
```

Expect the intended active account, project ID, a `RUNNING` cluster, and repository
format `DOCKER`. Stop if the repository is missing or uses another format. All
resource commands name the project explicitly instead of changing a global
default. See [repository inspection](https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/repositories/describe).

## 2. Configure and explicitly select kubectl

The GKE credential command needs `container.clusters.get`; applying workloads
also requires the appropriate Kubernetes permissions. Confirm the required auth
plugin is installed, then use a new private kubeconfig. See
[GKE client configuration](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl).

```sh
# Create a separate kubeconfig so the tutorial does not replace another cluster connection.
gke-gcloud-auth-plugin --version
umask 077
export WEAVE_PROVIDER_DIR="$HOME/weave-gcp-$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')"
mkdir -m 700 "$WEAVE_PROVIDER_DIR"
export KUBECONFIG="$WEAVE_PROVIDER_DIR/kubeconfig"
gcloud container clusters get-credentials "$WEAVE_GKE_CLUSTER" \
  --project "$WEAVE_GCP_PROJECT" --location "$WEAVE_GKE_LOCATION"
kubectl config get-contexts
```

Expect a context generated for the selected project, location, and cluster.
Inspect the list and copy that exact name:

```sh
# Confirm access to the intended namespace and identify the target CPU architecture.
export WEAVE_KUBE_CONTEXT='your-exact-context-name-from-the-list'
kubectl --context "$WEAVE_KUBE_CONTEXT" get namespaces
kubectl --context "$WEAVE_KUBE_CONTEXT" get nodes -L kubernetes.io/arch
```

Expect the intended namespaces and `amd64` or `arm64` node labels. Build for the
scheduled node architecture; ask the operator for the pool and placement rules
if your role cannot list nodes. Private endpoints require an approved network
path from this terminal. Credential acquisition can change the current context;
the remaining instructions always use the explicit selected name.

## 3. Configure the Docker credential helper

Authorize Docker for the exact Artifact Registry host. The command updates local
Docker credential-helper configuration; it does not create the repository or
change its permissions. See [Artifact Registry Docker authentication](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication).

```sh
# Register the credential helper for this exact registry host so Docker can push later.
export WEAVE_AR_HOST="$WEAVE_AR_LOCATION-docker.pkg.dev"
gcloud auth configure-docker "$WEAVE_AR_HOST"
export WEAVE_REGISTRY_PREFIX="$WEAVE_AR_HOST/$WEAVE_GCP_PROJECT/$WEAVE_AR_REPOSITORY"
```

Review the prompt and accept the named host. Expect confirmation that Docker
configuration was updated, or that the entry already exists. The prefix is the
existing Docker repository; the common guide appends `/server` and `/worker` as
image names. Use the same OS user for authentication and subsequent Docker calls.
Keep `WEAVE_DOCKER_CONTEXT` from the common guide's explicit local build selection.

The build operator needs repository writer access, such as
`roles/artifactregistry.writer`. The GKE node service account separately needs
`roles/artifactregistry.reader` on the repository; local authentication does not
grant it. For cross-project registries, grant read access in the registry's
project. See [Artifact Registry permissions](https://docs.cloud.google.com/artifact-registry/docs/access-control)
and [GKE node image-pull identity](https://docs.cloud.google.com/kubernetes-engine/security/configure-node-service-accounts).

## 4. Choose supporting services deliberately

| Requirement | Google Cloud option | Weave boundary |
| --- | --- | --- |
| PostgreSQL | Cloud SQL for PostgreSQL or separately operated PostgreSQL | Qualify exact migrations, role attributes and function ownership; local PostgreSQL tests do not certify Cloud SQL |
| Secret storage | Secret Manager through an operator-managed delivery mechanism | Configuring a delivery controller does not implement a Weave secret-provider port; this guide adds no native Secret Manager provider |
| Token issuer | Operated HTTPS Keycloak, or another separately qualified OIDC issuer | Verify issuer/audience/JWKS and create Weave local identity links/grants; Google Cloud IAM permissions are separate |

Cloud SQL's administrative users do not have unrestricted PostgreSQL superuser
powers. Rehearse the selected artifact against the intended service/version using
the [database qualification gate](kubernetes.md#2-prove-the-database-authority-model).
Keep migration ownership separate from runtime logins and do not run the local
fixture setup script against Cloud SQL. See
[Cloud SQL users and roles](https://docs.cloud.google.com/sql/docs/postgres/users).

## Continue with the shared deployment

Keep `KUBECONFIG`, `WEAVE_KUBE_CONTEXT`, and `WEAVE_REGISTRY_PREFIX` in this
terminal. Return to the [cloud deployment overview](cloud-deployment.md) to build,
push, and record exact registry digests. Then use the
[Kubernetes recipe](kubernetes.md) for database qualification, the migration Job,
API rollout, and a verified public run. These provider setup commands do not
publish a workflow or admit a worker release; the [CLI tutorial](../guides/cli-tutorial.md)
performs that separate application lifecycle.
