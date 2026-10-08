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
// Compile-only: the documented Execute and step test examples, typed. The
// Python suite parses the same bodies with the host and platform models.
import type {
  ExecuteRequest,
  ExecuteResponse,
  StepTestRequest,
  StepTestView,
} from "../../src/app/editor/state/execution";

export const executeRequest: ExecuteRequest = {
  source: "<yaml or json>",
  format: "yaml",
  definitions: [{ apiVersion: "weave/v1alpha1", kind: "Action", "...": "..." }],
  workflows: {
    "notify-customer@1.0.0": {
      source: "<yaml>",
      format: "yaml",
      mocks: { "node:send-mail": { id: "m-9" } },
    },
  },
  catalog: "project",
  refreshCatalog: false,
  stubUnresolved: true,
  input: { amount: 1500, orders: [{ id: "o-1" }, { id: "o-2" }] },
  mocks: { "node:check": { eligible: true }, "node:send[1]": { id: "m-1" } },
  scripts: {
    signals: { "wait-paid": { payload: { paid: true } } },
    humans: { approval: { decision: "approve", data: {} } },
    aiTurns: { support: { turns: [{ result: { text: "Shipped." } }] } },
    autoAdvanceWaits: true,
  },
  target: { mode: "step", stepId: "send", instanceKey: "send[1]" },
  now: "2026-10-07T10:00:00Z",
};

export const executeResponse: ExecuteResponse = {
  status: "blocked",
  compile: {
    ok: true,
    sliced: true,
    artifactDigest: "0".repeat(64),
    diagnostics: [],
    stubs: ["crm.lookup@1.0.0"],
  },
  trace: [
    {
      index: 0,
      nodeId: "check",
      instanceKey: "",
      kind: "action",
      status: "completed",
      input: { id: "c-1" },
      output: { eligible: true },
      virtualTime: "2026-10-07T10:00:00Z",
    },
    {
      index: 1,
      nodeId: "send",
      instanceKey: "send[0]",
      kind: "action",
      status: "completed",
      output: { id: "m-0" },
    },
    {
      index: 2,
      nodeId: "send-mail",
      instanceKey: "",
      frame: "enrich",
      kind: "action",
      status: "completed",
    },
  ],
  loops: [
    {
      nodeId: "notify",
      instanceKey: "",
      count: 2,
      completed: 1,
      running: 1,
      nextIndex: 2,
      status: "running",
    },
  ],
  frames: [
    {
      key: "enrich",
      workflow: "notify-customer@1.0.0",
      callNode: "enrich",
      status: "completed",
    },
  ],
  selectedScope: { item: { id: "o-2" }, index: 1, loops: {} },
  blocked: {
    reason: "missing_mock",
    nodeId: "notify",
    instanceKey: "",
    message: "Step notify needs test data",
  },
  variables: { status: "running", steps: {}, branches: {} },
  diagnostics: [],
};

export const stepTest: StepTestRequest = {
  kind: "action",
  step_id: "check-customer",
  uses: "onboarding.check-customer@1.0.0",
  input: { id: "c-1" },
  connections: { crm: "7a8c1f20-5b3d-4e6f-9a81-0c2d3e4f5a6b" },
  profile: null,
  acknowledged_side_effect: "non_idempotent",
  releases: { "weave-http@2.0.0": null },
  tool_mocks: {},
  real_tools: [],
  draft_id: "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
  draft_revision: 12,
  timeout_seconds: 120,
};

export const waitingStepTest: StepTestView = {
  id: "5c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  run_id: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
  status: "waiting",
  side_effect: "non_idempotent",
  pending: [
    {
      node_id: "support",
      instance_key: "support#2.1",
      kind: "confirm",
      tool: "refund_order",
      side_effect: "non_idempotent",
      arguments: { orderId: "A-17" },
    },
    {
      node_id: "support",
      instance_key: "support#3.1.review",
      kind: "review",
      tool: "send_reply",
      task_id: "6d7e8f90-1a2b-4c3d-9e8f-7a6b5c4d3e2f",
    },
    {
      node_id: "support",
      instance_key: "support#4.1",
      kind: "person",
      tool: "ask_manager",
      task_id: "7e8f9a0b-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
    },
  ],
  output: null,
  error: null,
  attempts: 1,
  started_at: "2026-10-07T10:00:00Z",
  ended_at: null,
  duration_ms: null,
};
