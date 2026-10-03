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
/** Mirrors the canonical contracts/deployments.py responses; no provider credentials. */
export type Adapter = "docker-compose" | "kubernetes" | "azure-container-apps";
export type Capability =
  | "observe"
  | "deploy"
  | "update"
  | "scale_workers"
  | "drain_workers";
export type JobState =
  | "queued"
  | "claimed"
  | "running"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "reconciliation_required";
export interface Scope {
  tenant_id: string;
  project_id: string | null;
  environment_id: string | null;
}
export interface TargetRequest {
  name: string;
  adapter: Adapter;
  external_identity: string;
  boundary: string;
  runner_principal_id: string;
  capabilities: Capability[];
}
export interface TargetUpdate {
  runner_principal_id: string;
  capabilities: Capability[];
  disabled: boolean;
}
export interface Target extends TargetRequest {
  id: string;
  scope: Scope;
  revision: number;
  disabled: boolean;
  created_at: string;
}
export interface ComponentSpec {
  name: string;
  kind: "api" | "worker" | "lumi" | "migration";
  image: string;
  configuration: string;
  replicas: number;
  cpu_millis: number;
  memory_mib: number;
  worker_release_id: string | null;
}
export interface DeploymentRequest {
  target_id: string;
  name: string;
  ownership: "imported" | "managed";
  components: ComponentSpec[];
}
export interface Deployment extends DeploymentRequest {
  id: string;
  scope: Scope;
  revision: number;
  created_at: string;
}
export interface ObservedResource {
  name: string;
  external_identity: string;
  kind: ComponentSpec["kind"] | "unknown";
  image: string | null;
  replicas: number;
  ready_replicas: number;
  version: string;
  ownership: "managed" | "imported" | "unknown";
  state: "ready" | "progressing" | "stopped" | "failed" | "unknown";
}
export interface Observation {
  id: string;
  target_id: string;
  target_revision: number;
  observed_at: string;
  expires_at: string;
  digest: string;
  resources: ObservedResource[];
  complete: boolean;
  settled: boolean;
}
export interface PlanStep {
  action: Exclude<Capability, "observe">;
  component: ComponentSpec;
  expected_version: string | null;
}
export interface Plan {
  id: string;
  scope: Scope;
  target_id: string;
  target_revision: number;
  deployment_id: string;
  deployment_revision: number;
  adapter: Adapter;
  adapter_version: "1";
  intent: Exclude<Capability, "observe">;
  observation_id: string;
  observation_digest: string;
  steps: PlanStep[];
  risks: (
    | "service_interruption"
    | "external_effects"
    | "adoption"
    | "worker_drain"
  )[];
  created_at: string;
  expires_at: string;
  digest: string;
}
export interface Approval {
  plan_id: string;
  digest: string;
  principal_id: string;
  approved_at: string;
}
export interface Receipt {
  code:
    | "observed"
    | "applied"
    | "unsupported"
    | "precondition_failed"
    | "provider_failed"
    | "cancelled"
    | "ambiguous"
    | "reconciled";
  changed_resources: string[];
  external_effects_may_continue: boolean;
}
export interface Job {
  id: string;
  scope: Scope;
  target_id: string;
  target_revision: number;
  kind: "observe" | "apply";
  plan_id: string | null;
  plan_digest: string | null;
  state: JobState;
  revision: number;
  operation_key: string;
  created_at: string;
  deadline: string;
  receipt: Receipt | null;
  observation_id: string | null;
  reconciliation_started_at: string | null;
}
export interface Runner {
  id: string;
  principal_id: string;
  target_id: string;
  adapter: Adapter;
  adapter_version: "1";
  capabilities: Capability[];
  last_seen: string;
  expires_at: string;
  revoked: boolean;
}
export interface Collections {
  targets: Target;
  deployments: Deployment;
  observations: Observation;
  plans: Plan;
  jobs: Job;
  runners: Runner;
}
export type Collection = keyof Collections;
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export const terminalJobs = new Set<JobState>([
  "succeeded",
  "failed",
  "cancelled",
  "reconciliation_required",
]);
export const adapterLabels: Record<Adapter, string> = {
  "docker-compose": "Docker / Compose",
  kubernetes: "Kubernetes",
  "azure-container-apps": "Azure Container Apps",
};
export function observationState(
  value: Observation | null,
  now = Date.now(),
): "unknown" | "incomplete" | "expired" | "current" {
  if (!value) return "unknown";
  if (!value.complete) return "incomplete";
  return Date.parse(value.expires_at) > now ? "current" : "expired";
}
