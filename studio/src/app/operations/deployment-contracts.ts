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
  | "drain_workers"
  | "restart_workers";
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
/** Whether a second person must approve; left out at its default (true). */
export interface ApprovalPolicy {
  distinct_approver: boolean;
}
export interface TargetRequest {
  name: string;
  adapter: Adapter;
  external_identity: string;
  boundary: string;
  runner_principal_id: string;
  capabilities: Capability[];
  /** Absent means a second person must approve. */
  approval_policy?: ApprovalPolicy;
}
export interface TargetUpdate {
  runner_principal_id: string;
  capabilities: Capability[];
  disabled: boolean;
  /** Absent keeps the target's policy; changing it also needs environment.manage. */
  approval_policy?: ApprovalPolicy;
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
  kind: "api" | "worker" | "lumi";
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
  kind: ComponentSpec["kind"] | "migration" | "unknown";
  image: string | null;
  replicas: number;
  ready_replicas: number;
  version: string;
  ownership: "managed" | "imported" | "unknown";
  state: "ready" | "progressing" | "stopped" | "failed" | "unknown";
  /**
   * The containers or pods of a worker component (100 or fewer), as the
   * runner saw them; left out when empty. Placement only: never identity or
   * authority.
   */
  members?: ObservedMember[];
}
/** One container or pod of an observed worker component. */
export interface ObservedMember {
  /** The hostname its worker reports: a container ID prefix or a pod name. */
  host: string;
  ordinal: number | null;
  /** The runner's own lowercase state, such as running or pending. */
  state: string;
  ready: boolean;
  started_at: string | null;
  restarts: number;
  node: string | null;
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
  /** "2": the runner removes exactly the reviewed workers on a scale-down. */
  adapter_version: "1" | "2";
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

/** Change requests: one reviewed infrastructure change that outlives its plans. */
export type ChangeIntent =
  | "deploy"
  | "update"
  | "scale_workers"
  | "restart_workers";
export type ChangeState =
  | "observing"
  | "ready_to_plan"
  | "awaiting_approval"
  | "approved"
  | "draining"
  | "applying"
  | "succeeded"
  | "failed"
  | "reconciliation_required"
  | "expired"
  | "declined"
  | "cancelled"
  | "superseded";
/** What a change sets on one component; absent fields keep the deployment's value. */
export interface DesiredComponent {
  name: string;
  replicas?: number;
  image?: string;
  worker_release_id?: string;
  cpu_millis?: number;
  memory_mib?: number;
}
export interface DrainSetting {
  mode: "none" | "drain_first";
  timeout_seconds: number;
}
export interface ChangeRequestCreate {
  target_id: string;
  deployment_id: string;
  expected_deployment_revision: number;
  intent: ChangeIntent;
  components: string[];
  desired?: DesiredComponent[];
  drain: DrainSetting;
  title: string;
  reason: string;
  plan_ttl_seconds?: number;
}
export interface DrainMember {
  worker_id: string;
  host: string;
  active_leases: number;
}
export interface ChangeRequest
  extends Omit<ChangeRequestCreate, "plan_ttl_seconds"> {
  id: string;
  scope: Scope;
  revision: number;
  requested_by: string;
  created_at: string;
  plan_ttl_seconds: number | null;
  deployment_revision: number;
  approval_policy: ApprovalPolicy;
  state: ChangeState;
  observation_job_id: string | null;
  observation_id: string | null;
  plan_id: string | null;
  plan_digest: string | null;
  plan_expires_at: string | null;
  approval: { principal_id: string; approved_at: string } | null;
  planned_by: string | null;
  drain_set: DrainMember[];
  drained: string[];
  apply_job_id: string | null;
  failure: { code: string; message: string } | null;
  warnings: string[];
  history: { at: string; state: ChangeState; actor_id: string | null }[];
}
export interface DrainProgressMember extends DrainMember {
  draining: boolean;
  presence: "recent" | "stale" | "unknown";
  revoked: boolean;
}
export interface DrainProgress {
  /** Present only for someone who may read the change's target. */
  change_request?: ChangeRequest;
  members: DrainProgressMember[];
  remaining: number;
}
