/*
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
*/
import type {
  Adapter,
  Capability,
  ComponentSpec,
  ObservedResource,
  Target,
} from "./deployment-contracts";

export type ComponentDraft = Omit<
  ComponentSpec,
  "cpu_millis" | "memory_mib"
> & { cpu_millis: number | null; memory_mib: number | null };

export interface RunnerApplication {
  value: string;
  label: string;
}
export interface AdmittedWorkerRelease {
  id: string;
  image_digest: string;
  capabilities: { taskType: string; taskVersion: string }[];
}
export function supportedCapabilities(adapter: Adapter): Capability[] {
  return adapter === "docker-compose"
    ? ["observe", "deploy", "update", "scale_workers"]
    : ["observe", "update", "scale_workers"];
}
export function supportedPlanComponents(
  adapter: Adapter,
  intent: Capability,
  components: Pick<ComponentSpec, "kind">[],
): boolean {
  const selected = components.filter(
    (item) => intent !== "scale_workers" || item.kind === "worker",
  );
  return (
    selected.length > 0 &&
    (adapter !== "azure-container-apps" ||
      selected.every((item) => item.kind === "worker" || item.kind === "lumi"))
  );
}
export function runnerSetup(target: Target, baseUrl: string) {
  return {
    base_url: baseUrl,
    scope: target.scope,
    oauth_file: "/opt/weave/private/oauth.json",
    poll_seconds: 2,
    destination: {
      target_id: target.id,
      adapter: target.adapter,
      external_identity: target.external_identity,
      boundary: target.boundary,
      executable:
        "/usr/local/bin/" +
        {
          "docker-compose": "docker",
          kubernetes: "kubectl",
          "azure-container-apps": "az",
        }[target.adapter],
      context: "REPLACE_WITH_LOCAL_CONTEXT_OR_SUBSCRIPTION",
      capabilities: ["observe"],
      components: [],
      image_repositories: [],
      ...(target.adapter === "docker-compose"
        ? { compose_file: "/opt/weave/private/compose.yaml" }
        : {}),
      ...(target.adapter !== "kubernetes"
        ? { lock_file: "/opt/weave/private/runner.lock" }
        : {}),
    },
  };
}
export function observedComponents(
  resources: ObservedResource[],
): ComponentDraft[] {
  return resources.flatMap((resource) =>
    resource.kind !== "unknown" &&
    resource.kind !== "migration" &&
    resource.image &&
    /^.+@sha256:[a-f0-9]{64}$/.test(resource.image)
      ? [
          {
            name: resource.name,
            kind: resource.kind,
            image: resource.image,
            replicas: resource.replicas,
            configuration: "",
            cpu_millis: null,
            memory_mib: null,
            worker_release_id: null,
          },
        ]
      : [],
  );
}
