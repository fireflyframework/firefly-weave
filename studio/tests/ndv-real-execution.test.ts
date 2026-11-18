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
import { describe, expect, it } from "vitest";
import { actionKind } from "../src/app/editor/ndv/kinds/builtin";
import type {
  KindContext,
  NdvContext,
  SampleValue,
} from "../src/app/editor/ndv/registry";
import { sideEffectOf } from "../src/app/editor/state/step-test-service";
import type { Json } from "../src/app/forms/core/json";
import type { Step, Workflow } from "../src/app/model";

const REVISION = "11111111-2222-4333-8444-555555555555";
const contract = (spec: Record<string, unknown>): Json => ({
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "acme.send-invoice", version: "1.0.0" },
  spec: { inputSchema: { type: "object" }, ...spec } as Json,
});

function context(
  step: Partial<Step>,
  over: {
    input?: SampleValue;
    bound?: boolean;
    contract?: Json | null;
  } = {},
): { ctx: NdvContext; kind: KindContext } {
  const full = {
    id: "send-invoice",
    kind: "action",
    uses: "acme.send-invoice@1.0.0",
    connection: "crm",
    with: { to: { ref: "/steps/lookup/output/email" } },
    ...step,
  } as Step;
  const workflow = {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-intake", version: "1.0.0" },
    spec: {
      connections: { crm: { connector: "weave-http@2.0.0" } },
      steps: [full],
    },
  } as unknown as Workflow;
  const ctx = {
    step: full,
    workflow,
    sample: {
      resolvedInput: () =>
        over.input ?? {
          state: "available",
          value: { to: "ana@acme.test" },
          source: "simulated",
          stale: null,
        },
    },
    connections: {
      slot: (name: string) =>
        name === "crm"
          ? { connector: "weave-http@2.0.0", required: true }
          : null,
      devBinding: (slot: string) =>
        slot === "crm" && over.bound !== false
          ? {
              connectionId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
              revisionId: REVISION,
              name: "acme-crm",
              tested: null,
            }
          : null,
    },
  } as unknown as NdvContext;
  const kind = {
    workflow,
    features: [],
    actionContract: () =>
      over.contract === undefined
        ? contract({ sideEffect: "non_idempotent", timeoutSeconds: 45 })
        : over.contract,
    tableContract: () => null,
    workflowContract: () => null,
  } as KindContext;
  return { ctx, kind };
}

describe("executing an action step for real", () => {
  it("an action step's request carries its resolved input and bound revision", () => {
    const { ctx, kind } = context({});
    expect(actionKind.real?.request(ctx, kind)).toEqual({
      body: {
        kind: "action",
        step_id: "send-invoice",
        uses: "acme.send-invoice@1.0.0",
        input: { to: "ana@acme.test" },
        connections: { crm: REVISION },
        releases: {},
        timeout_seconds: 45,
      },
    });
  });

  it("an action without a connection or input sends none", () => {
    const { ctx, kind } = context({ connection: undefined, with: undefined });
    expect(actionKind.real?.request(ctx, kind)).toMatchObject({
      body: { input: {}, connections: {} },
    });
  });

  it("says what's missing first", () => {
    const blocked = (step: Partial<Step>, over = {}) => {
      const { ctx, kind } = context(step, over);
      return actionKind.real?.request(ctx, kind);
    };
    expect(blocked({ uses: undefined })).toEqual({
      blocked: "Choose an action first.",
    });
    expect(blocked({}, { bound: false })).toEqual({
      blocked: "Choose the connection for slot crm first.",
    });
    expect(blocked({}, { input: { state: "none" } })).toEqual({
      blocked:
        "send-invoice needs data from earlier steps. Execute previous steps first.",
    });
    expect(
      blocked({}, { input: { state: "omitted", reason: "secret" } }),
    ).toEqual({
      blocked:
        "send-invoice needs data from earlier steps. Execute previous steps first.",
    });
  });

  it("caps the timeout at 300 seconds", () => {
    const long = context({}, { contract: contract({ timeoutSeconds: 900 }) });
    expect(actionKind.real?.request(long.ctx, long.kind)).toMatchObject({
      body: { timeout_seconds: 300 },
    });
    const unknown = context({}, { contract: null });
    expect(actionKind.real?.request(unknown.ctx, unknown.kind)).toMatchObject({
      body: { timeout_seconds: 300 },
    });
  });

  it("reads the side effect only from the contract's typed field", () => {
    expect(sideEffectOf(contract({ sideEffect: "non_idempotent" }))).toBe(
      "non_idempotent",
    );
    expect(sideEffectOf(contract({ sideEffect: "read_only" }))).toBe(
      "read_only",
    );
    expect(sideEffectOf(contract({ sideEffect: "maybe" }))).toBeNull();
    expect(sideEffectOf({ sideEffect: "non_idempotent" })).toBeNull();
    expect(sideEffectOf(null)).toBeNull();
  });
});
