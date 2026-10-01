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

# Prepare Azure AKS and ACR

Use an **existing operator-provisioned AKS cluster** and Azure Container Registry
(ACR). Install Azure CLI, kubectl, Docker, Python 3, and the authentication tooling
required by your cluster, including kubelogin for Microsoft Entra integration.
Authenticate Azure CLI with your approved operator identity before starting.
The commands below select resources and configure clients; they create no cluster.

![Registry, Kubernetes, database, identity, and Weave deployment boundaries](../diagrams/cloud-deployment.svg)

Read ACR as the image boundary and AKS as the runtime boundary. Registry push
access, node pull access, and Weave application grants are separate permissions.
[Open the diagram at full size](../diagrams/cloud-deployment.svg).

## If you do not have infrastructure yet

Use the provider's [Azure cluster creation tutorial](https://learn.microsoft.com/en-us/azure/aks/tutorial-kubernetes-deploy-cluster?tabs=azure-cli) to prepare an approved cluster and its network,
node capacity, and access controls. Provision the registry and intended namespace
through the same infrastructure process. Then return to step 1 with the actual
resource names. These resources incur charges; a provider quickstart is a learning
baseline, not a production availability or security design for Weave.

## 1. Select the subscription and inspect resources

Replace the placeholders with values from the existing deployment inventory.
The resource group below contains the cluster; ACR is selected by its existing
registry name in the same subscription.

```sh
# Select the intended subscription and verify the existing cluster and registry names.
export WEAVE_AZURE_SUBSCRIPTION='your-subscription-id'
export WEAVE_AZURE_RESOURCE_GROUP='your-cluster-resource-group'
export WEAVE_AKS_CLUSTER='your-existing-cluster'
export WEAVE_ACR_NAME='your-existing-registry-name'
az account set --subscription "$WEAVE_AZURE_SUBSCRIPTION"
az account show --query '{subscription:id,tenant:tenantId,name:name}' --output json
az aks show --resource-group "$WEAVE_AZURE_RESOURCE_GROUP" --name "$WEAVE_AKS_CLUSTER" \
  --query '{name:name,location:location,state:provisioningState}' --output json
az acr show --name "$WEAVE_ACR_NAME" \
  --query '{id:id,location:location,loginServer:loginServer}' --output json
```

Check the subscription, tenant, resource names, and regions against the operator's
inventory; expect AKS provisioning state `Succeeded`. ACR's returned `loginServer`
is authoritative: use it instead of constructing a hostname from a guessed suffix.
See the [ACR CLI reference](https://learn.microsoft.com/en-us/cli/azure/acr?view=azure-cli-latest).

## 2. Configure and explicitly select kubectl

Use ordinary cluster-user credentials and a new private kubeconfig. Retrieving
them needs permission to obtain AKS user credentials; Kubernetes authorization is
a separate check. Follow the [AKS credential command](https://learn.microsoft.com/en-us/cli/azure/aks?view=azure-cli-latest#az-aks-get-credentials)
for your cluster's authentication mode.

```sh
# Save ordinary cluster-user credentials in a private kubeconfig for this installation.
umask 077
export WEAVE_PROVIDER_DIR="$HOME/weave-azure-$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')"
mkdir -m 700 "$WEAVE_PROVIDER_DIR"
export KUBECONFIG="$WEAVE_PROVIDER_DIR/kubeconfig"
az aks get-credentials --resource-group "$WEAVE_AZURE_RESOURCE_GROUP" \
  --name "$WEAVE_AKS_CLUSTER" --file "$KUBECONFIG"
kubectl config get-contexts
```

Expect a context for this AKS cluster. Inspect it and copy the exact context name:

```sh
# Check the exact Kubernetes context and node architecture before building images.
export WEAVE_KUBE_CONTEXT='your-exact-context-name-from-the-list'
kubectl --context "$WEAVE_KUBE_CONTEXT" get namespaces
kubectl --context "$WEAVE_KUBE_CONTEXT" get nodes -L kubernetes.io/arch
```

Expect your namespaces and the intended Linux pool's `amd64` or `arm64` labels.
Use that architecture and placement in the shared image/deployment steps. If node
listing is restricted, obtain the pool architecture from the operator. Your
terminal must have a network route to a private cluster endpoint.

## 3. Authenticate Docker and verify pull authority

`az acr login` uses the authenticated Azure identity and Docker client. Select the
local Docker build context first; login does not deploy a workload. See
[ACR authentication](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication).

```sh
# Let Docker authenticate to the registry using your Azure identity; cluster pull access is separate.
: "${WEAVE_DOCKER_CONTEXT:?Select the local build context in the cloud overview}"
export WEAVE_ACR_LOGIN_SERVER="$(az acr show --name "$WEAVE_ACR_NAME" --query loginServer --output tsv)"
DOCKER_CONTEXT="$WEAVE_DOCKER_CONTEXT" az acr login --name "$WEAVE_ACR_NAME"
export WEAVE_REGISTRY_PREFIX="$WEAVE_ACR_LOGIN_SERVER/weave"
```

Expect `Login Succeeded`. The common build/push guide creates image repositories
`weave/server` and `weave/worker` through their first authorized pushes. The
operator needs push rights; the AKS kubelet identity separately needs pull rights.
For conventional registry RBAC, these are commonly `AcrPush` and `AcrPull`.
For registries using repository ABAC, use the matching repository writer/reader
permissions. In particular, `az aks --attach-acr` does not support ABAC-enabled
registries; the platform administrator must configure the appropriate repository
reader assignment. This chapter does not change IAM assignments. See
[AKS–ACR integration and its ABAC limitation](https://learn.microsoft.com/en-us/azure/aks/cluster-container-registry-integration).

## 4. Choose supporting services deliberately

| Requirement | Azure option | Weave boundary |
| --- | --- | --- |
| PostgreSQL | Azure Database for PostgreSQL Flexible Server or separately operated PostgreSQL | Exact migration/role ownership rehearsal is required; local PostgreSQL evidence does not certify Flexible Server |
| Secret storage | Azure Key Vault with an operator-managed delivery mechanism | Delivery into configured processes is separate from a Weave secret-provider implementation; this guide installs no native Key Vault provider |
| Token issuer | Operated HTTPS Keycloak, or separately qualified Microsoft Entra/OIDC integration | Verify the actual token contract and local identity links/grants; AKS access alone does not authorize Weave |

Flexible Server's `azure_pg_admin` is restricted. Apply the
[database qualification gate](kubernetes.md#2-prove-the-database-authority-model)
before choosing it: the selected migration identity must satisfy packaged role
attributes, grants, policy creation, and function ownership without weakening RLS.
Do not run the local fixture setup script there. See
[Azure PostgreSQL access control](https://learn.microsoft.com/en-us/azure/postgresql/security/security-access-control).

## Continue with the shared deployment

Keep `KUBECONFIG`, `WEAVE_KUBE_CONTEXT`, and `WEAVE_REGISTRY_PREFIX` in this
terminal. Return to the [cloud deployment overview](cloud-deployment.md) to build,
push, and record exact registry digests. Then use the
[Kubernetes recipe](kubernetes.md) for database qualification, the migration Job,
API rollout, and a verified public run. These provider setup commands do not
publish a workflow or admit a worker release; the [CLI tutorial](../guides/cli-tutorial.md)
performs that separate application lifecycle.
