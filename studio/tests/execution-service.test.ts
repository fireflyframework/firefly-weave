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
// The execution service: one execution at a time, an answer applied only to
// the draft and revision it was asked for, and results kept only on this
// computer under the account's prefix.
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../src/app/api";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import {
  ExecutionService,
  type ExecutionHost,
} from "../src/app/editor/state/execution-service";
import type { StepFingerprints } from "../src/app/editor/state/fingerprints";
import { sampleFromSchema } from "../src/app/editor/ndv/panes/sample";
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

afterEach(() => vi.useRealTimers());

describe("the execution service", () => {
  it("applies an answer only to the draft and revision it was asked for", async () => {
    const { service, sent, values, state } = fixture();
    await service.open("d-1");
    const first = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    expect((await first).kind).toBe("done");
    const before = service.results();
    // Another draft opens while the host is still answering.
    const second = service.execute({ mode: "workflow" });
    await settle();
    state.draft = { id: "d-2", revision: 1 };
    await service.open("d-2");
    sent[1].answer({ ...RESPONSE, trace: [] });
    expect((await second).kind).toBe("stale");
    expect(service.results()).toBeNull();
    expect(values.has("p:s:t:pr:weave.lastExecution.d-2")).toBe(false);
    // The same draft, edited meanwhile.
    state.draft = { id: "d-1", revision: 1 };
    await service.open("d-1");
    expect(service.results()).toEqual(before);
    const third = service.execute({ mode: "workflow" });
    await settle();
    state.draft = { id: "d-1", revision: 2 };
    sent[2].answer({ ...RESPONSE, trace: [] });
    expect((await third).kind).toBe("stale");
    expect(service.results()).toEqual(before);
  });

  it("drops an answer for steps renamed or deleted meanwhile", async () => {
    const { service, sent, state } = fixture();
    await service.open("d-1");
    const running = service.execute({ mode: "workflow" });
    await settle();
    state.prints = new Map([
      ["check-customer", print(1)],
      ["send", print(2)],
    ]);
    sent[0].answer(RESPONSE);
    expect((await running).kind).toBe("stale");
    expect(service.results()).toBeNull();
  });

  it("runs one execution at a time", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    const first = service.execute({ mode: "workflow" });
    await settle();
    expect(service.running()).toEqual({ mode: "workflow" });
    expect(await service.execute({ mode: "step", stepId: "send" })).toEqual({
      kind: "busy",
    });
    expect(sent).toHaveLength(1);
    sent[0].answer(RESPONSE);
    await first;
    expect(service.running()).toBeNull();
  });

  it("keeps results only under a known account, and in memory otherwise", async () => {
    const { service, sent, values, state } = fixture();
    await service.open("d-1");
    const kept = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    await kept;
    const text = values.get("p:s:t:pr:weave.lastExecution.d-1");
    expect(JSON.parse(text!).steps.check.output).toEqual({ ok: true });
    values.clear();
    state.prefix = null;
    await service.open("d-1");
    const memory = service.execute({ mode: "workflow" });
    await settle();
    sent[1].answer(RESPONSE);
    await memory;
    expect([...values.keys()]).toEqual([]);
    expect(service.results()?.steps["check"]?.output).toEqual({ ok: true });
  });

  it("reads the kept results back when the draft opens again", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    const kept = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    await kept;
    const results = service.results();
    service.forget();
    expect(service.results()).toBeNull();
    await service.open("d-1");
    expect(service.results()).toEqual(results);
  });

  it("drops results larger than 1 MiB instead of keeping half", async () => {
    const { service, sent, values } = fixture();
    await service.open("d-1");
    const big = service.execute({ mode: "workflow" });
    await settle();
    const output = "x".repeat(2 * 1024 * 1024);
    sent[0].answer({ ...RESPONSE, trace: [{ ...RESPONSE.trace[0], output }] });
    await big;
    expect(values.has("p:s:t:pr:weave.lastExecution.d-1")).toBe(false);
    expect(service.results()?.steps["check"]?.output).toBe(output);
  });

  it("turns host refusals into plain outcomes and never repeats a value", async () => {
    vi.useFakeTimers();
    const { service, sent, state } = fixture();
    state.testData = {
      ...EMPTY,
      input: { value: { id: "s3cr3t" }, source: "manual" },
    };
    await service.open("d-1");
    const refuse = async (error: ApiError, retried = false) => {
      const outcome = service.execute({ mode: "workflow" });
      await vi.advanceTimersByTimeAsync(0);
      sent.at(-1)!.refuse(error);
      if (retried) {
        await vi.advanceTimersByTimeAsync(500);
        sent.at(-1)!.refuse(error);
      }
      return outcome;
    };
    const secret = await refuse(
      new ApiError(422, {
        status: 422,
        code: "WV-SCHEMA-SECRET_VALUE",
        message: "Test data can't hold values marked secret; leave them out",
        result: { paths: ["/input"] },
      }),
    );
    expect(secret).toMatchObject({
      kind: "refused",
      code: "WV-SCHEMA-SECRET_VALUE",
      paths: ["/input"],
    });
    expect(JSON.stringify(secret)).not.toContain("s3cr3t");
    expect(
      await refuse(
        new ApiError(409, { status: 409, code: "WV-STUDIO-CATALOG-CONFLICT" }),
      ),
    ).toEqual({ kind: "conflict" });
    expect(
      await refuse(
        new ApiError(502, { status: 502, code: "WV-STUDIO-CATALOG" }),
      ),
    ).toEqual({ kind: "catalog" });
    const before = sent.length;
    expect(
      await refuse(
        new ApiError(503, { status: 503, code: "WV-STUDIO-BUSY" }),
        true,
      ),
    ).toEqual({ kind: "busy" });
    expect(sent.length - before).toBe(2);
    expect(service.running()).toBeNull();
  });

  it("sends a sample of the input fields when there is no test input", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    const running = service.execute({ mode: "workflow" });
    await settle();
    expect(sent[0].path).toBe("/studio/local/debug/execute");
    expect((sent[0].body as { input: unknown }).input).toEqual(
      sampleFromSchema(INPUT_SCHEMA),
    );
    expect((sent[0].body as { catalog: string }).catalog).toBe("none");
    sent[0].answer(RESPONSE);
    await running;
    expect(service.sampledInput()).toBe(true);
  });

  it("Stop drops the running answer", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    const running = service.execute({ mode: "workflow" });
    await settle();
    service.stop();
    expect(service.running()).toBeNull();
    sent[0].answer(RESPONSE);
    expect((await running).kind).toBe("stale");
    expect(service.results()).toBeNull();
  });

  it("steps through a session with the revision it was given", async () => {
    const { service, sent } = fixture();
    const created = service.sessions.create();
    await settle();
    expect(sent[0].path).toBe("/studio/local/debug/sessions");
    sent[0].answer({ id: "s-1", revision: 1, view: { status: "running" } });
    expect((await created)?.revision).toBe(1);
    const moved = service.sessions.command({ kind: "next" });
    await settle();
    expect(sent[1].path).toBe("/studio/local/debug/sessions/s-1/commands");
    expect(sent[1].headers["If-Match"]).toBe('"1"');
    sent[1].answer({ id: "s-1", revision: 2, view: { status: "paused" } });
    expect((await moved)?.revision).toBe(2);
    const closed = service.sessions.close();
    await settle();
    expect([sent[2].path, sent[2].method]).toEqual([
      "/studio/local/debug/sessions/s-1",
      "DELETE",
    ]);
    sent[2].answer(undefined);
    await closed;
    expect(service.sessions.current()).toBeNull();
  });
  it("never uses another draft's results, even before that draft's own open", async () => {
    const { service, sent, state, values } = fixture();
    await service.open("d-1");
    const first = service.execute({ mode: "workflow" });
    await settle();
    sent[0].answer(RESPONSE);
    await first;
    // Draft B shows before its results are read (its test data is still on the way).
    state.draft = { id: "d-2", revision: 1 };
    const second = service.execute({ mode: "workflow" });
    await settle();
    expect((sent[1].body as { mocks: object }).mocks).toEqual({});
    sent[1].answer({ ...RESPONSE, trace: [] });
    expect((await second).kind).toBe("done");
    expect(service.results()?.steps).toEqual({});
    expect(
      JSON.parse(values.get("p:s:t:pr:weave.lastExecution.d-2")!).steps,
    ).toEqual({});
  });

  it("says the host took too long instead of retrying it as busy", async () => {
    const { service, sent } = fixture();
    await service.open("d-1");
    const slow = service.execute({ mode: "workflow" });
    await settle();
    sent[0].refuse(
      new ApiError(503, {
        status: 503,
        code: "WV-STUDIO-TIMEOUT",
        message: "Executing took longer than 30 seconds; execute fewer steps",
      }),
    );
    expect(await slow).toEqual({
      kind: "failed",
      message: "Executing took longer than 30 seconds; execute fewer steps",
    });
    expect(sent).toHaveLength(1);
  });

  it("after Stop, a busy host is finishing the stopped execution", async () => {
    vi.useFakeTimers();
    const { service, sent } = fixture();
    await service.open("d-1");
    void service.execute({ mode: "workflow" });
    await vi.advanceTimersByTimeAsync(0);
    service.stop();
    const again = service.execute({ mode: "workflow" });
    await vi.advanceTimersByTimeAsync(0);
    const busy = new ApiError(503, { status: 503, code: "WV-STUDIO-BUSY" });
    sent[1].refuse(busy);
    await vi.advanceTimersByTimeAsync(500);
    sent[2].refuse(busy);
    expect(await again).toEqual({ kind: "busy", stopping: true });
  });
});
