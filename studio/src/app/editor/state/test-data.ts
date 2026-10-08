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
// Test data, schema urn:firefly-weave:schema:studio-test-data:v1: one document
// per workflow with input, pins, scripts, AI turns and called-workflow mocks.
// The JSON Schema in src/firefly_weave/contracts/schemas/studio-test-data-v1.json
// is normative; these types follow it field for field (camelCase). The
// platform envelope that stores the document is a snake_case API body.
import type { InstanceKey, Json } from "../ndv/registry";

export const TEST_DATA_SCHEMA_ID =
  "urn:firefly-weave:schema:studio-test-data:v1" as const;
export const TEST_DATA_MAX_BYTES = 1_048_576;
export const CANVAS_MAX_BYTES = 262_144;

export type DataSource =
  | "manual"
  | "schema"
  | "simulated"
  | "test-call"
  | "run";
/** `fnv1a64:` and 16 lowercase hex digits. */
export type Fingerprint = `fnv1a64:${string}`;
/** UUIDs; data from a run or a test call names the run and its environment. */
export interface Provenance {
  runId?: string;
  runEnvironmentId?: string;
}
export interface InputEntry extends Provenance {
  value: Json;
  source: DataSource;
}
export interface PinEntry extends Provenance {
  output: Json;
  source: DataSource;
  pinnedAt: string;
  configHash: Fingerprint;
  contractHash: Fingerprint;
  upstreamHash: Fingerprint;
}
export interface MockEntry extends Provenance {
  output: Json;
  source: DataSource;
}
/** A frame key: `enrich`, `notify[2]/enrich`. */
export type FrameKey = string;
export interface SignalScript extends Provenance {
  payload: Json;
  afterSeconds?: number;
  frame?: FrameKey;
  source?: DataSource;
  outcome?: never;
}
export interface SignalTimeout {
  outcome: "timeout";
  frame?: FrameKey;
  payload?: never;
}
export type SignalEntry = SignalScript | SignalTimeout;
export interface HumanScript extends Provenance {
  decision: string;
  data: Json;
  frame?: FrameKey;
  source?: DataSource;
  outcome?: never;
}
export interface HumanExpire {
  outcome: "expire";
  frame?: FrameKey;
  decision?: never;
  data?: never;
}
export type HumanEntry = HumanScript | HumanExpire;
export interface AiMemoryMessage {
  role: "user" | "assistant";
  content: Json;
}
/** Recorded agent turns; each item of `turns` follows urn:firefly-weave:schema:studio-ai-turns:v1. */
export interface AiTurnsEntry extends Provenance {
  turns: { [key: string]: Json }[];
  memory?: AiMemoryMessage[];
  autoApprove?: boolean;
  source?: "manual" | "simulated" | "test-call" | "run";
  configHash?: Fingerprint;
  upstreamHash?: Fingerprint;
}
export interface CalleeMocks {
  mocks: Record<InstanceKey, MockEntry>;
}
export interface WaitScripts {
  /** true when absent. */
  autoAdvance?: boolean;
}
/** Keys are step IDs (every iteration) or instance keys (one iteration or activation). */
export interface StudioTestData {
  schemaVersion: 1;
  kind: "weave.studio/testData";
  updatedAt: string;
  input?: InputEntry;
  pins?: Record<InstanceKey, PinEntry>;
  signals?: Record<InstanceKey, SignalEntry>;
  humans?: Record<InstanceKey, HumanEntry>;
  aiTurns?: Record<InstanceKey, AiTurnsEntry>;
  /** Mocks for steps inside called workflows, by callee reference (`notify-customer@1.0.0`). */
  workflows?: Record<string, CalleeMocks>;
  waits?: WaitScripts;
}

export type StudioDocumentKind = "canvas" | "test-data";
/**
 * `{PROJECT}/drafts/{identifier}/studio/{kind}`; the server derives the
 * run-data fields. `Document` is `StudioTestData` for kind "test-data".
 */
export interface StudioDocument<
  Document extends object = { [key: string]: Json },
> {
  draft_id: string;
  kind: StudioDocumentKind;
  revision: number;
  draft_revision: number;
  document: Document;
  contains_run_data: boolean;
  source_environment_ids: string[];
  updated_at: string;
  updated_by: string;
}
/** The save body: a client can neither assert nor clear run data. */
export interface StudioDocumentSave<
  Document extends object = { [key: string]: Json },
> {
  draft_revision: number;
  document: Document;
}
