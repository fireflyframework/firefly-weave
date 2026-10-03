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

# Prepare AWS EKS and ECR

This guide connects your terminal to an **existing EKS cluster** and to private
**ECR** repositories, so that the shared guides can build, push, and deploy Weave.
It configures your client tools only; it creates no cloud infrastructure.

**Who this is for:** an operator with an approved AWS profile, access to the
cluster, permission to push to ECR, and a network path to the Kubernetes API
endpoint.

**What you need first:** AWS CLI v2, `kubectl`, Docker, and Python 3, with the AWS
CLI signed in through your organization's approved profile. In the same terminal,
load your local installation's `session.env` as described in the
[cloud deployment overview](cloud-deployment.md#1-choose-the-infrastructure-route),
so that `WEAVE_DOCKER_CONTEXT` is set.

**What you will have at the end:** `KUBECONFIG`, `WEAVE_KUBE_CONTEXT`,
`WEAVE_KUBE_NAMESPACE`, and `WEAVE_REGISTRY_PREFIX` set in this terminal.

![Registry, Kubernetes, database, identity, and Weave deployment boundaries](../diagrams/cloud-deployment.svg)

ECR sits at the image boundary (step 2 in the diagram) and EKS runs the green
panel. Your AWS access to those services is separate from the Weave principal and
grants that the API checks.
[Open diagram at full size](../diagrams/cloud-deployment.svg)

## If you do not have infrastructure yet

Use the provider's [AWS cluster creation guide](https://docs.aws.amazon.com/eks/latest/userguide/create-cluster.html)
to prepare an approved cluster with its network, node capacity, and access
controls. Provision the registry and the intended namespace through the same
infrastructure process, then return to step 1 with the real resource names. These
resources cost money, and a provider quickstart is a learning baseline, not a
production availability or security design for Weave.

## 1. Select the account and actual resources

**Why:** every later command depends on the right account, region, and cluster.
Replace every `your-*` value with the existing resource names from your operator.
This example uses the standard AWS commercial partition and an ECR registry in the
same account as the signed-in operator.

```sh
# Select the AWS account and region, then inspect the existing cluster before changing local configuration.
export AWS_PROFILE='your-approved-profile'
export AWS_REGION='your-cluster-and-registry-region'
export WEAVE_EKS_CLUSTER='your-existing-cluster'
aws sts get-caller-identity
aws eks describe-cluster --region "$AWS_REGION" --name "$WEAVE_EKS_CLUSTER" \
  --query 'cluster.{name:name,arn:arn,status:status,endpoint:endpoint}' --output json
```

Expected: the account you intended, and the cluster with status `ACTIVE`. Check
that the cluster ARN matches the intended environment. Reading the cluster needs
`eks:DescribeCluster`; using Kubernetes also needs the cluster's access mapping
and RBAC, because an AWS identity alone grants no Kubernetes permissions. See
[AWS cluster access instructions](https://docs.aws.amazon.com/eks/latest/userguide/create-kubeconfig.html).

## 2. Configure and explicitly select kubectl

**Why:** a separate, private kubeconfig keeps your other cluster connections
untouched. AWS's command writes the configuration and selects its new context;
the commands below still name that context explicitly. See
[update-kubeconfig](https://docs.aws.amazon.com/cli/latest/reference/eks/update-kubeconfig.html).

```sh
# Keep this cluster connection in its own private kubeconfig so your other contexts are preserved.
umask 077
export WEAVE_PROVIDER_DIR="$HOME/weave-aws-$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')"
mkdir -m 700 "$WEAVE_PROVIDER_DIR"
export KUBECONFIG="$WEAVE_PROVIDER_DIR/kubeconfig"
aws eks update-kubeconfig --region "$AWS_REGION" --name "$WEAVE_EKS_CLUSTER" \
  --kubeconfig "$KUBECONFIG"
kubectl config get-contexts
```

Expected: a context named after the selected EKS cluster ARN. Copy its exact name
into `WEAVE_KUBE_CONTEXT` below, never an unrelated current context. Then name the
namespace reserved for Weave and check what you may do there:

```sh
# Select the exact context and namespace, then confirm access and the node CPU architecture.
export WEAVE_KUBE_CONTEXT='your-exact-context-name-from-the-list'
export WEAVE_KUBE_NAMESPACE='your-existing-namespace'
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" auth can-i create deployments
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" auth can-i create jobs
kubectl --context "$WEAVE_KUBE_CONTEXT" -n "$WEAVE_KUBE_NAMESPACE" auth can-i create secrets
kubectl --context "$WEAVE_KUBE_CONTEXT" get nodes -L kubernetes.io/arch
```

Expected: `yes` three times, then the nodes with an architecture label such as
`amd64` or `arm64`. Build for that architecture in the shared guide: an image
built on an Apple silicon laptop is not automatically an AMD64 image. If your role
cannot list nodes, ask the operator for the node pool's architecture and
placement rules. A private cluster needs an approved network path from this
terminal.

## 3. Authenticate Docker to existing repositories

**Why:** the shared guide pushes `weave/server` and, for a remote worker,
`weave/worker`. Each needs an existing ECR repository, which the registry
administrator creates with the right push and pull policies. Docker signs in
with a token passed directly from the AWS CLI, as in
[AWS's ECR push procedure](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html).

```sh
# Authenticate Docker to the existing ECR repositories; this uploads no images yet.
export WEAVE_AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export WEAVE_ECR_HOST="$WEAVE_AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
aws ecr describe-repositories --region "$AWS_REGION" \
  --repository-names weave/server weave/worker \
  --query 'repositories[].repositoryUri' --output json
: "${WEAVE_DOCKER_CONTEXT:?Load your local session.env as described in the cloud overview}"
aws ecr get-login-password --region "$AWS_REGION" | \
  docker --context "$WEAVE_DOCKER_CONTEXT" login \
  --username AWS --password-stdin "$WEAVE_ECR_HOST"
export WEAVE_REGISTRY_PREFIX="$WEAVE_ECR_HOST/weave"
```

Expected: both repository URIs, then `Login Succeeded`. Stop if a repository you
need is missing. For an API-only rollout, remove `weave/worker` from the
`describe-repositories` command: it reports an error for a repository that does
not exist.

**Pushing and pulling are separate permissions.** The build operator needs ECR
authentication and image upload rights. The EKS nodes, or the Fargate pod
execution role, need their own ECR read access: your Docker sign-in on a laptop
gives the cluster nothing. A registry in another account needs that registry
account selected explicitly, with matching policies, instead of the same-account
address built above.

## 4. Choose supporting services deliberately

| Requirement | AWS option | Weave boundary |
| --- | --- | --- |
| PostgreSQL | RDS for PostgreSQL or separately operated PostgreSQL | Rehearse the exact migrations and role and function ownership on the chosen service; local PostgreSQL tests do not certify RDS |
| Secret storage | AWS Secrets Manager through an operator-managed delivery mechanism | Delivering values into processes is not a Weave secret-provider implementation; this guide installs no native AWS provider |
| Token issuer | Your compatible HTTPS OIDC/CIAM provider ([configuration](identity-and-secrets.md#use-your-own-identity-provider)) | Configure and verify issuer, audience, and JWKS, and create Weave identity links and grants; AWS IAM access is not a Weave grant |

**RDS's administrative role is not an unrestricted PostgreSQL superuser.** Before
you deploy, pass the [database qualification gate](kubernetes.md#2-prove-the-database-authority-model),
including role creation, grants, exact role attributes, and function ownership.
Never run the local fixture setup script against RDS. See
[AWS's rds_superuser restrictions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.rds_superuser.html).

## If something goes wrong

| What you see | Why | What to do |
| --- | --- | --- |
| `aws eks describe-cluster` is denied | The selected AWS identity lacks `eks:DescribeCluster`, or `AWS_PROFILE` selects another account | Check `aws sts get-caller-identity` and ask for the permission |
| `kubectl` reports `You must be logged in to the server (Unauthorized)` | Your AWS identity has no access entry or RBAC in this cluster | Ask the cluster administrator to grant Kubernetes access to your identity |
| `kubectl` times out | The cluster endpoint is private and this terminal has no route to it | Use the approved network path, such as a VPN or bastion |
| `auth can-i` prints `no` | Your Kubernetes role cannot create that object in the namespace | Ask for the namespace permissions before you continue |
| `describe-repositories` reports a missing repository | `weave/server` or `weave/worker` does not exist in this account and region | Ask the registry administrator to create it |
| Pods later stay in `ImagePullBackOff` | The nodes' role cannot read the ECR repository | Grant ECR read access to the node or Fargate pod execution role |

## Continue with the shared deployment

Keep `KUBECONFIG`, `WEAVE_KUBE_CONTEXT`, `WEAVE_KUBE_NAMESPACE`, and
`WEAVE_REGISTRY_PREFIX` in this terminal, then:

1. Return to the [cloud deployment overview](cloud-deployment.md#2-pass-the-infrastructure-readiness-gate)
   to pass the readiness gate, then build, push, and record the registry digests.
2. Use the [Kubernetes walkthrough](kubernetes.md) for database qualification, the
   migration Job, the API rollout, and a verified run.

These provider commands neither publish a workflow nor admit a worker release.
The [CLI tutorial](../guides/cli-tutorial.md) covers that separate application
lifecycle.
