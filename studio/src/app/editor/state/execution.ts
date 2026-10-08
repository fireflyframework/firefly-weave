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
// Execute step, Execute previous steps and Execute workflow: the simulated
// bodies of the Studio host (/studio/local/*, camelCase) and the real bodies of
// the platform (step tests, draft test activations, the environment test
// policy; snake_case). Python models: firefly_weave.studio.local_contracts and
// firefly_weave.contracts.step_tests.
import type { Diagnostic } from "../../api";
import type { InstanceKey, Json } from "../ndv/registry";
import type {
  AiTurnsEntry,
  FrameKey,
  HumanEntry,
  SignalEntry,
  StudioTestData,
} from "./test-data";

type JsonObject = { [key: string]: Json };

// Studio host, simulated (camelCase).

/** `node:<instance key or step ID>`, `agent:<instance key or step ID>`, `tool:<instance key or step ID>/<tool>`, `action:<ref>`, most specific first. */
export type MockKey =
  | `node:${string}`
  | `action:${string}`
  | `agent:${string}`
  | `tool:${string}/${string}`;
export interface CalleeSource {
  source?: string;
  format?: "yaml" | "json";
  mocks?: Record<MockKey, Json>;
}
export interface ExecuteScripts {
  signals?: Record<InstanceKey, SignalEntry>;
  humans?: Record<InstanceKey, HumanEntry>;
  aiTurns?: Record<InstanceKey, AiTurnsEntry>;
  /** true when absent. */
  autoAdvanceWaits?: boolean;
}
export type ExecuteMode = "workflow" | "step" | "before";
export interface ExecuteTarget {
  mode: ExecuteMode;
  stepId?: string;
  /** One iteration of a step inside loops; iteration 0 when absent. */
  instanceKey?: InstanceKey;
}
/** `POST /studio/local/debug/sessions`; Execute adds `target`. */
export interface SimulationRequest {
  source: string;
  format: "yaml" | "json";
  definitions?: JsonObject[];
  workflows?: Record<string, CalleeSource>;
  catalog: "project" | "none";
  refreshCatalog?: boolean;
  stubUnresolved?: boolean;
  input?: Json;
  mocks?: Record<MockKey, Json>;
  scripts?: ExecuteScripts;
  now: string;
}
/** `POST /studio/local/debug/execute`. */
export interface ExecuteRequest extends SimulationRequest {
  target: ExecuteTarget;
}
export type ExecuteStatus = "completed" | "blocked" | "failed" | "limit";
export type TraceStatus = "running" | "waiting" | "completed" | "failed";
export interface CompileSummary {
  ok: boolean;
  sliced: boolean;
  artifactDigest?: string | null;
  diagnostics: Diagnostic[];
  stubs: string[];
}
/** `instanceKey` is "" when it equals the step ID. */
export interface TraceEntry {
  index: number;
  nodeId: string;
  instanceKey: InstanceKey | "";
  kind: string;
  status: TraceStatus;
  input?: Json;
  output?: Json;
  virtualTime?: string;
  frame?: FrameKey;
}
export interface LoopProgress {
  nodeId: string;
  instanceKey: InstanceKey | "";
  count: number;
  completed: number;
  running: number;
  nextIndex: number;
  status: "running" | "completed" | "failed";
}
export interface FrameView {
  key: FrameKey;
  workflow: string;
  callNode: InstanceKey;
  status: TraceStatus;
}
export interface LoopRoot {
  item: Json;
  index: number;
}
export interface SelectedScope {
  item?: Json;
  index?: number;
  loops?: Record<string, LoopRoot>;
}
export type BlockedReason =
  | "missing_mock"
  | "signal"
  | "human"
  | "path"
  | "callee";
export interface Blocked {
  reason: BlockedReason;
  nodeId: string;
  instanceKey: InstanceKey | "";
  message: string;
}
export interface ExecuteResponse {
  status: ExecuteStatus;
  compile: CompileSummary;
  trace: TraceEntry[];
  loops?: LoopProgress[];
  frames?: FrameView[];
  selectedScope?: SelectedScope;
  blocked?: Blocked;
  variables?: JsonObject;
  /** Debugger errors, such as a missing mock; problems with the definition are in `compile.diagnostics`. */
  diagnostics?: Diagnostic[];
}
export type CheckCode =
  | "WV-SCHEMA-SECRET_VALUE"
  | "WV-SCHEMA-CLASSIFICATION"
  | "WV-STUDIO-TESTDATA-SCHEMA"
  | "WV-STUDIO-TESTDATA-UNRESOLVED";
/** `POST /studio/local/testdata/check`. */
export interface CheckTestDataRequest {
  source: string;
  format: "yaml" | "json";
  definitions?: JsonObject[];
  catalog: "project" | "none";
  testData: StudioTestData;
}
export interface CheckTestDataEntry {
  /** A JSON pointer into the test data, e.g. "/pins/check". */
  path: string;
  ok: boolean;
  code?: CheckCode;
}
export interface CheckTestDataResult {
  entries: CheckTestDataEntry[];
}
/** `POST /studio/local/decision/evaluate`. */
export interface DecisionEvaluateRequest {
  table: JsonObject;
  input: Json;
}
export interface DecisionEvaluation {
  output: Json;
  matched_rule_ids: string[];
  used_default: boolean;
}

// Platform, in an environment (snake_case).

export type SideEffect =
  | "read_only"
  | "idempotent"
  | "idempotency_key"
  | "non_idempotent";
/** `POST {ENV}/step-tests`. */
export interface StepTestRequest {
  kind: string;
  step_id: string;
  uses?: string | null;
  input: Json;
  connections?: Record<string, string>;
  profile?: string | null;
  acknowledged_side_effect?: SideEffect | null;
  /** Dependency reference to a release ID; null lets the server pick the only admitted release. */
  releases?: Record<string, string | null>;
  tool_mocks?: Record<string, Json>;
  real_tools?: string[];
  draft_id: string;
  draft_revision: number;
  timeout_seconds: number;
}
export interface StepTestAccepted {
  id: string;
  run_id: string;
  status: "running";
  side_effect: SideEffect;
  expires_at: string;
}
export type PendingKind = "confirm" | "review" | "person";
export interface StepTestPending {
  node_id: string;
  instance_key: InstanceKey | "";
  kind: PendingKind;
  tool?: string;
  side_effect?: SideEffect;
  /** The tool's own input; it keeps that input schema's casing. */
  arguments?: JsonObject;
  task_id?: string;
}
/** The UI shows `cancelled` as "Canceled". */
export type StepTestStatus =
  | "running"
  | "waiting"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "cancelled";
export interface StepTestView {
  id: string;
  run_id: string;
  status: StepTestStatus;
  side_effect: SideEffect;
  pending: StepTestPending[];
  output: Json;
  error: { code: string; message: string } | null;
  attempts: number;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
}
/** `POST {ENV}/step-tests/{id}/answers`. */
export interface StepTestAnswer {
  instance_key: InstanceKey;
  confirm: boolean;
}
export interface DraftTestBindings {
  connection_revision_ids?: Record<string, string>;
  connector_release_ids?: Record<string, string>;
  worker_release_ids?: Record<string, string>;
  assignment_binding_ids?: Record<string, string>;
  workflow_activation_ids?: Record<string, string>;
}
/** `POST {ENV}/test-activations`. */
export interface DraftTestActivationRequest {
  draft_id: string;
  draft_revision: number;
  bindings?: DraftTestBindings;
  acknowledged_side_effect?: SideEffect | null;
  /** 900 when absent; at most 3,600. */
  lifetime_seconds?: number;
}
export interface DraftTestActivation {
  id: string;
  activation_id: string;
  artifact_digest: string;
  side_effect: SideEffect;
  expires_at: string;
}
/** `POST {ENV}/test-activations/{id}/runs`. */
export interface DraftTestRunRequest {
  input: Json;
}
/** `POST {ENV}/test-activations/{id}/listen`. */
export interface DraftTestListenRequest {
  secret_ref: string;
  max_body_bytes?: number;
  tolerance_seconds?: number;
}
export interface DraftTestListen {
  test_url: string;
  expires_at: string;
}
export type StepTestCalls = "enabled" | "disabled";
/** `GET {ENV}/test-policy`. */
export interface EnvironmentTestPolicy {
  test_calls: StepTestCalls;
  revision: number;
}
/** `PUT {ENV}/test-policy` with `If-Match`. */
export interface EnvironmentTestPolicyUpdate {
  test_calls: StepTestCalls;
}
