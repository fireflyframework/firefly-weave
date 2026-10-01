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

Use this chapter with an **existing operator-provisioned EKS cluster** and private
ECR repositories. Install AWS CLI v2, kubectl, Docker, and Python 3; authenticate
the AWS CLI through your organization's approved profile. You need cluster
access, ECR push permission, and network access to the Kubernetes endpoint.
This chapter configures your client; it creates no cloud infrastructure.

![Registry, Kubernetes, database, identity, and Weave deployment boundaries](../diagrams/cloud-deployment.svg)

Locate ECR at the image boundary and EKS at the runtime boundary. AWS access to
those services is separate from the local Weave principal and grants checked by
the API. [Open the diagram at full size](../diagrams/cloud-deployment.svg).

## If you do not have infrastructure yet

Use the provider's [AWS cluster creation guide](https://docs.aws.amazon.com/eks/latest/userguide/create-cluster.html) to prepare an approved cluster and its network,
node capacity, and access controls. Provision the registry and intended namespace
through the same infrastructure process. Then return to step 1 with the actual
resource names. These resources incur charges; a provider quickstart is a learning
baseline, not a production availability or security design for Weave.

## 1. Select the account and actual resources

Replace every `your-*` value with the existing resource information supplied by
your operator. This example uses the standard AWS commercial partition and an
ECR registry in the same account as the authenticated operator.

```sh
# Select the AWS account and region, then inspect the existing cluster before changing local configuration.
export AWS_PROFILE='your-approved-profile'
export AWS_REGION='your-cluster-and-registry-region'
export WEAVE_EKS_CLUSTER='your-existing-cluster'
aws sts get-caller-identity
aws eks describe-cluster --region "$AWS_REGION" --name "$WEAVE_EKS_CLUSTER" \
  --query 'cluster.{name:name,arn:arn,status:status,endpoint:endpoint}' --output json
```

Check that the returned account and cluster ARN match the intended environment;
expect cluster status `ACTIVE`. Retrieving configuration requires
`eks:DescribeCluster`; Kubernetes access also requires the configured cluster
access mapping and RBAC. An AWS identity alone does not grant Kubernetes
permissions. See [AWS cluster access instructions](https://docs.aws.amazon.com/eks/latest/userguide/create-kubeconfig.html).

## 2. Configure and explicitly select kubectl

Create a private, separate kubeconfig. AWS's command writes local configuration
and selects its new context; subsequent commands below still name the inspected
context explicitly. See [update-kubeconfig](https://docs.aws.amazon.com/cli/latest/reference/eks/update-kubeconfig.html).

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

Expect an added context for the selected EKS ARN. Inspect the list, then copy its
exact name into the following variable; do not copy an unrelated current context.

```sh
# Confirm Kubernetes access and the CPU architecture that your image must support.
export WEAVE_KUBE_CONTEXT='your-exact-context-name-from-the-list'
kubectl --context "$WEAVE_KUBE_CONTEXT" get namespaces
kubectl --context "$WEAVE_KUBE_CONTEXT" get nodes -L kubernetes.io/arch
```

Expect the intended namespaces and node architecture labels such as `amd64` or
`arm64`. Build for the selected node architecture in the shared guide; a local
Apple Silicon build is not automatically an AMD64 image. If node listing is
restricted, have the operator supply the intended pool architecture and placement
rules. Private clusters require an approved network path from this terminal.

## 3. Authenticate Docker to existing repositories

The common image names require two existing ECR repositories: `weave/server` and
`weave/worker`. The registry administrator provisions them and the appropriate
push/pull policies. Authenticate using a token passed directly to Docker, as in
[AWS's ECR push procedure](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html).

```sh
# Authenticate Docker to the existing ECR repositories; this uploads no images yet.
export WEAVE_AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export WEAVE_ECR_HOST="$WEAVE_AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
aws ecr describe-repositories --region "$AWS_REGION" \
  --repository-names weave/server weave/worker \
  --query 'repositories[].repositoryUri' --output json
: "${WEAVE_DOCKER_CONTEXT:?Select the local build context in the cloud overview}"
aws ecr get-login-password --region "$AWS_REGION" | \
  docker --context "$WEAVE_DOCKER_CONTEXT" login \
  --username AWS --password-stdin "$WEAVE_ECR_HOST"
export WEAVE_REGISTRY_PREFIX="$WEAVE_ECR_HOST/weave"
```

Expect both repository URIs and `Login Succeeded`. Stop if either repository is
missing. The build operator needs ECR authentication and image upload permissions;
EKS node or Fargate image-pull identity needs its own ECR read access. Docker login
on your laptop does not grant cluster image pulls. Cross-account registries need
an explicitly selected registry account and corresponding policies instead of
this same-account derivation.

## 4. Choose supporting services deliberately

| Requirement | AWS option | Weave boundary |
| --- | --- | --- |
| PostgreSQL | RDS for PostgreSQL or separately operated PostgreSQL | Rehearse exact migrations and role/function ownership on the chosen service; local PostgreSQL tests do not certify RDS |
| Secret storage | AWS Secrets Manager through an operator-managed delivery mechanism | Supplying environment/configuration is distinct from implementing a Weave secret-provider port; no native AWS provider is installed by this guide |
| Token issuer | Operated HTTPS Keycloak, or a separately qualified OIDC issuer | Configure and verify issuer/audience/JWKS and provision Weave identity links and grants; AWS IAM access is not a Weave grant |

RDS's administrative role is not unrestricted PostgreSQL superuser access.
Before deployment, apply the [database qualification gate](kubernetes.md#2-prove-the-database-authority-model),
including role creation, grants, exact role attributes, and function ownership.
Do not run the local fixture setup script against RDS. See
[AWS's rds_superuser restrictions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.rds_superuser.html).

## Continue with the shared deployment

Keep `KUBECONFIG`, `WEAVE_KUBE_CONTEXT`, and `WEAVE_REGISTRY_PREFIX` in this
terminal. Return to the [cloud deployment overview](cloud-deployment.md) to build,
push, and record exact registry digests. Then use the
[Kubernetes recipe](kubernetes.md) for database qualification, the migration Job,
API rollout, and a verified public run. These provider setup commands do not
publish a workflow or admit a worker release; the [CLI tutorial](../guides/cli-tutorial.md)
performs that separate application lifecycle.
