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
// A step's real result from Execute in an environment, as the execution
// service keeps it: in memory only, replaced by the next execution.
import { describe, expect, it } from "vitest";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import {
  ExecutionService,
  type ExecutionHost,
} from "../src/app/editor/state/execution-service";
import type { StepFingerprints } from "../src/app/editor/state/fingerprints";
import type { StudioTestData } from "../src/app/editor/state/test-data";
import type { Workflow } from "../src/app/model";
import { RESPONSE, print } from "./execution-fixtures";

const WORKFLOW = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "orders", version: "1.0.0" },
  spec: {
    steps: [
      { id: "check", kind: "action", uses: "crm.check@1.0.0", with: {} },
      { id: "send", kind: "action", uses: "mail.send@1.0.0", with: {} },
    ],
  },
} as unknown as Workflow;
const EMPTY: StudioTestData = {
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: "2026-10-26T10:00:00Z",
};
const INPUT_SCHEMA = {
  type: "object",
  properties: { id: { type: "string" }, amount: { type: "number" } },
};

interface Pending {
  path: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
  answer: (value: unknown) => void;
  refuse: (error: unknown) => void;
}

function fixture() {
  const values = new Map<string, string>();
  const backend: StoreBackend = {
    keys: async (prefix) =>
      [...values.keys()].filter((key) => key.startsWith(prefix)),
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
  const sent: Pending[] = [];
  const state = {
    draft: { id: "d-1", revision: 1 },
    prefix: "p:s:t:pr:" as string | null,
    prints: new Map<string, StepFingerprints>([
      ["check", print(1)],
      ["send", print(2)],
    ]),
    testData: EMPTY,
  };
  const host: ExecutionHost = {
    api: {
      request: <T>(
        path: string,
        method = "GET",
        body?: unknown,
        headers: Record<string, string> = {},
      ) =>
        new Promise<T>((answer, refuse) =>
          sent.push({
            path,
            method,
            body,
            headers,
            answer: answer as (value: unknown) => void,
            refuse,
          }),
        ),
    },
    backend,
    now: () => new Date("2026-10-26T12:04:00Z"),
    draft: () => ({
      id: state.draft.id,
      revision: state.draft.revision,
      source: "{}",
      format: "json",
      workflow: WORKFLOW,
    }),
    place: () => (state.prefix === null ? null : { prefix: state.prefix }),
    connected: () => false,
    definitions: () => [],
    testData: () => state.testData,
    prints: () => state.prints,
    inputSchema: () => INPUT_SCHEMA,
  };
  return { service: new ExecutionService(host), sent, values, state };
}

/** Lets the service reach its next await. */
const settle = () => new Promise((done) => setTimeout(done, 0));
describe("a real result from a step test", () => {
  const real = {
    id: "3f2a9c1e-1111-4222-8333-444455556666",
    run_id: "9d8c7b6a-1111-4222-8333-444455556666",
    status: "succeeded" as const,
    side_effect: "read_only" as const,
    pending: [],
    output: { marker: "REAL-OUTPUT-7f3" },
    error: null,
    attempts: 1,
    started_at: "2026-10-26T12:00:00Z",
    ended_at: "2026-10-26T12:00:01Z",
    duration_ms: 1000,
  };
  const local = { id: "env-local", name: "local" };

  it("is kept in memory and never written to this computer", async () => {
    const { service, sent, values } = fixture();
    await service.open("d-1");
    const kept = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    await kept;
    service.recordTestCall("send", real, local, print(2).execution, 30);
    expect(service.results()?.steps["send"]).toMatchObject({
      status: "completed",
      output: { marker: "REAL-OUTPUT-7f3" },
      real: {
        runId: real.run_id,
        environmentId: "env-local",
        environmentName: "local",
        status: "succeeded",
      },
    });
    // The next execution writes results again: the real one stays out of them.
    const again = service.execute({ mode: "step", stepId: "check" });
    await settle();
    sent[1].answer({
      ...RESPONSE,
      trace: RESPONSE.trace.filter((t) => t.node_id === "check"),
    });
    await again;
    for (const text of values.values())
      expect(text).not.toContain("REAL-OUTPUT-7f3");
    expect(service.results()?.steps["send"]?.real?.runId).toBe(real.run_id);
  });

  it("an output the platform left out keeps no value", () => {
    const { service } = fixture();
    service.recordTestCall(
      "send",
      { ...real, output: null, omitted: "classified_secret" },
      local,
      print(2).execution,
    );
    const result = service.results()?.steps["send"];
    expect(result?.output).toBeUndefined();
    expect(result?.real?.omitted).toBe("classified_secret");
  });

  it("a simulated execution replaces it", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    service.recordTestCall("send", real, local, print(2).execution);
    const simulated = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    await simulated;
    expect(service.results()?.steps["send"]?.real).toBeUndefined();
  });
});
