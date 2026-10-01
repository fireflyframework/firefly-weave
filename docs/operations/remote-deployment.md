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

# Remote deployment, one stage at a time

**Goal:** understand the route from a working local installation to a Weave API
running on shared infrastructure. This page is the map; the linked chapters
contain the commands for each stage.

You need an operator account for your infrastructure. Running `weave` on your
laptop does not create a cloud cluster. In this release, application deployments
use Kubernetes manifests; your cloud's infrastructure tooling supplies the
cluster, registry, database, and network.

## Before you open a cloud terminal

Complete [the local platform walkthrough](../guides/local-platform.md), or use an
existing Weave instance to publish and run one workflow. This establishes what a
working API looks like. Local object IDs and credentials belong to that local
installation; the remote installation will have its own.

Choose the scope of your first remote deployment:

- **API first:** deploy the API and run an internal transform workflow. Add
  workers after the API, database, and identity checks work.
- **API plus integrations:** also build, admit, and start the workers required
  by your workflow. Follow the optional worker stages in order.
- **Worker only:** when your team already operates Weave, deploy your worker
  with its API URL and admitted release. Follow [worker setup](../guides/workers.md)
  and the [Kubernetes worker section](kubernetes.md#7-add-a-remote-worker-only-when-required).

## The names in the deployment guides

| Name | What it does | What you supply |
| --- | --- | --- |
| Cluster | Machines and a Kubernetes control plane that run containers | An existing EKS, AKS, or GKE cluster |
| Context | A named connection in your local `kubectl` configuration | The exact context for that cluster |
| Namespace | A boundary for application objects inside the cluster | The namespace reserved for Weave |
| Registry | Storage from which cluster nodes download images | ECR, ACR, or Artifact Registry |
| Image digest | An immutable identifier for one published image | The digest returned after pushing |
| Migration Job | A short-lived process that creates or upgrades the schema | The packaged migration command and owner credentials |
| API Deployment | The long-running Weave HTTP service | Server image and runtime configuration |
| Worker Deployment | Your integration code claiming tasks through the API | Worker image, release ID, scope, and credentials |
| Service / ingress | Internal routing / your controlled external entry point | Service routing plus approved HTTPS/DNS configuration |

![Images delivered to the cluster; services using database and identity](../diagrams/cloud-deployment.svg)

## Follow these six stages

| Stage | Do this | Ready to continue when… |
| --- | --- | --- |
| 1. Connect | Use exactly one provider guide: [AWS](aws.md), [Azure](azure.md), or [Google Cloud](gcp.md) | You have the intended cluster context and registry access |
| 2. Prepare dependencies | Follow [infrastructure readiness](cloud-deployment.md#2-pass-the-infrastructure-readiness-gate) | Database migration permissions, runtime logins, identity, and network are ready |
| 3. Package | Follow [image build](cloud-deployment.md#3-build-images-for-the-target-architecture) and [push](cloud-deployment.md#4-push-and-record-registry-identities) | Required images have recorded registry digests |
| 4. Install | Follow [Kubernetes steps 1–5](kubernetes.md) | The migration Job succeeds and API readiness passes |
| 5. Prove one run | Follow [identity and workflow verification](kubernetes.md#6-verify-identity-and-one-public-workflow) | An authorized request produces a saved successful run |
| 6. Add external work | Follow [worker deployment](kubernetes.md#7-add-a-remote-worker-only-when-required) when your workflow needs it | An admitted worker completes the intended task |

Do one stage at a time. In stage 1, replace `your-*` placeholders with values
from your actual cloud inventory. In later stages, commands read variables and
receipt files produced earlier; a guessed UUID or image digest will not work.

## Two independent connections from your laptop

`kubectl` talks to Kubernetes to manage processes. `weave` talks to the running
Weave API to manage workflows. Their credentials and permissions are separate.

```sh
# Inspect the Kubernetes target before any deployment command.
# This reads your local configuration and does not apply manifests.
kubectl config get-contexts

# Read worker packaging help; this also makes no deployment changes.
weave worker package --help

# Open the guide for application deployment and the next required steps.
weave docs deploy
```

A successful container rollout only says the process started. The API's readiness,
identity, grant, and first-run checks establish that you can actually use it.
[Try the API](../guides/api-playground.md) explains enabling Swagger on your own
instance and using a token for interactive requests. Keep it behind your normal
network and access controls.

## What to save after deployment

Keep the selected package version, image digests, rendered manifests, private
configuration locations, and the IDs returned by bootstrap and the first run.
Use your team's secret store for credentials. These records allow another
operator to understand and maintain the installation.

For normal operations, use [configuration](configuration.md),
[observability](observability.md), [upgrades](upgrades.md), and
[backup/restore](backup-restore.md). Scaling replicas, changing an image, and
migrating the database are separate operations with different recovery needs.

## What has been verified

The templates and examples are checked against Weave's implementation. They are
not a completed deployment in your cloud account. Database ownership, identity,
network access, recovery, and real integration delivery must be verified in the
chosen environment. The local developer stack does not prove production availability.
