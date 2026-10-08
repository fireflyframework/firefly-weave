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
// Compile-only: the documented test data examples, typed. The Python suite
// validates the same documents against the JSON Schema.
import type {
  HumanEntry,
  SignalEntry,
  StudioDocument,
  StudioTestData,
} from "../../src/app/editor/state/test-data";

export const testData: StudioTestData = {
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: "2026-10-07T10:00:00Z",
  input: {
    value: { customerId: "c-1", orders: [{ id: "o-1" }, { id: "o-2" }] },
    source: "manual",
  },
  pins: {
    "check-customer": {
      output: { eligible: true },
      source: "test-call",
      pinnedAt: "2026-10-07T10:01:00Z",
      configHash: "fnv1a64:0123456789abcdef",
      contractHash: "fnv1a64:1111111111111111",
      upstreamHash: "fnv1a64:fedcba9876543210",
      runId: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
      runEnvironmentId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
    },
    send: {
      output: { id: "m-0" },
      source: "manual",
      pinnedAt: "2026-10-07T10:02:00Z",
      configHash: "fnv1a64:aaaaaaaaaaaaaaaa",
      contractHash: "fnv1a64:bbbbbbbbbbbbbbbb",
      upstreamHash: "fnv1a64:cccccccccccccccc",
    },
    "send[1]": {
      output: { id: "m-1" },
      source: "manual",
      pinnedAt: "2026-10-07T10:02:00Z",
      configHash: "fnv1a64:aaaaaaaaaaaaaaaa",
      contractHash: "fnv1a64:bbbbbbbbbbbbbbbb",
      upstreamHash: "fnv1a64:cccccccccccccccc",
    },
  },
  signals: { "wait-paid": { payload: { paid: true }, afterSeconds: 60 } },
  humans: {
    approval: { decision: "approve", data: {} },
    review: {
      decision: "reject",
      data: { note: "Wrong address" },
      frame: "enrich",
    },
  },
  aiTurns: {
    support: {
      turns: [{ result: { text: "Your order shipped." } }],
      autoApprove: true,
      source: "manual",
    },
  },
  workflows: {
    "notify-customer@1.0.0": {
      mocks: { "send-mail": { output: { id: "m-9" }, source: "manual" } },
    },
  },
  waits: { autoAdvance: true },
};

export const envelope: StudioDocument<StudioTestData> = {
  draft_id: "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
  kind: "test-data",
  revision: 3,
  draft_revision: 12,
  document: testData,
  contains_run_data: true,
  source_environment_ids: ["9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718"],
  updated_at: "2026-10-07T10:05:00Z",
  updated_by: "5d6e7f80-1a2b-4c3d-8e9f-0a1b2c3d4e5f",
};

// The schema takes a script or an outcome in a signal or human entry, never both.
// @ts-expect-error a timeout carries no payload
export const mixedSignal: SignalEntry = { outcome: "timeout", payload: {} };
// @ts-expect-error an expired task carries no data
export const mixedHuman: HumanEntry = { outcome: "expire", data: null };
