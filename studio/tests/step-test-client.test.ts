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
import { ApiError, type StudioApi } from "../src/app/api";
import type {
  StepTestRequest,
  StepTestView,
} from "../src/app/editor/state/execution";
import {
  FOLLOW_GRACE_MS,
  POLL_FAST_FOR_MS,
  POLL_FAST_MS,
  POLL_SLOW_MS,
  StepTestClient,
  readStepTestView,
  refusalOf,
} from "../src/app/editor/state/step-test-client";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const ID = "3f2a9c1e-1111-4222-8333-444455556666";
const RUN = "9d8c7b6a-1111-4222-8333-444455556666";
const view = (over: Partial<StepTestView> = {}): StepTestView => ({
  id: ID,
  run_id: RUN,
  status: "running",
  side_effect: "read_only",
  pending: [],
  output: null,
  error: null,
  attempts: 1,
  started_at: "2026-11-17T10:00:00Z",
  ended_at: null,
  duration_ms: null,
  ...over,
});
const body: StepTestRequest = {
  kind: "action",
  step_id: "lookup",
  uses: "acme.lookup@1.0.0",
  input: { id: 1 },
  connections: { crm: "11111111-2222-4333-8444-555555555555" },
  releases: {},
  draft_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  draft_revision: 4,
  timeout_seconds: 30,
};

interface Call {
  path: string;
  method: string;
  body?: unknown;
  headers?: Record<string, string>;
}
function fakeApi(answer: (call: Call, index: number) => unknown) {
  const calls: Call[] = [];
  const api = {
    environment: ENV,
    request: async (
      path: string,
      method = "GET",
      payload?: unknown,
      headers?: Record<string, string>,
    ) => {
      const call = { path, method, body: payload, headers };
      calls.push(call);
      const value = answer(call, calls.length - 1);
      if (value instanceof Error) throw value;
      return value;
    },
  } as unknown as StudioApi;
  return { api, calls };
}
function fakeClock() {
  let now = 0;
  const waits: number[] = [];
  return {
    waits,
    clock: {
      now: () => now,
      wait: async (ms: number) => {
        waits.push(ms);
        now += ms;
      },
    },
    advance(ms: number) {
      now += ms;
    },
  };
}
const problem = (status: number, code: string, result?: unknown) =>
  new ApiError(status, { code, message: `${code} said no.`, result });

describe("the step test client", () => {
  it("starts with the click's key and asks again with the same key when the answer is lost", async () => {
    const accepted = {
      id: ID,
      run_id: RUN,
      status: "running",
      side_effect: "read_only",
      expires_at: "2026-11-17T10:05:00Z",
    };
    const { api, calls } = fakeApi((_call, index) =>
      index === 0 ? problem(502, "WV-STUDIO-UPSTREAM") : accepted,
    );
    let checking = 0;
    const client = new StepTestClient(api, fakeClock().clock);
    const started = await client.start(body, "key-1", () => checking++);
    expect(started).toEqual({ ok: true, accepted });
    expect(checking).toBe(1);
    expect(calls).toHaveLength(2);
    for (const call of calls) {
      expect(call.path).toBe(`${ENV}/step-tests`);
      expect(call.method).toBe("POST");
      expect(call.body).toEqual(body);
      expect(call.headers).toEqual({ "Idempotency-Key": "key-1" });
    }
  });

  it("asks again only once", async () => {
    const { api, calls } = fakeApi(() => problem(502, "WV-STUDIO-UPSTREAM"));
    const client = new StepTestClient(api, fakeClock().clock);
    expect(await client.start(body, "key-1")).toEqual({
      ok: false,
      refusal: { kind: "lost" },
    });
    expect(calls).toHaveLength(2);
  });

  it("follows every second for 30 seconds, then every 2 seconds", async () => {
    const { clock, waits } = fakeClock();
    let reads = 0;
    const { api, calls } = fakeApi(() =>
      ++reads < 40 ? view() : view({ status: "succeeded", output: { n: 1 } }),
    );
    const seen: string[] = [];
    const result = await new StepTestClient(api, clock).follow(
      ID,
      300,
      (v) => seen.push(v.status),
      new AbortController().signal,
    );
    expect(result).toEqual(view({ status: "succeeded", output: { n: 1 } }));
    expect(POLL_FAST_MS).toBe(1000);
    expect(POLL_FAST_FOR_MS).toBe(30_000);
    expect(POLL_SLOW_MS).toBe(2000);
    expect(waits).toEqual([
      ...Array<number>(30).fill(1000),
      ...Array<number>(9).fill(2000),
    ]);
    expect(seen).toHaveLength(39);
    expect(calls[0]).toEqual({
      path: `${ENV}/step-tests/${ID}`,
      method: "GET",
      body: undefined,
      headers: undefined,
    });
  });

  it("stops waiting at the timeout plus a minute", async () => {
    const { clock } = fakeClock();
    const { api, calls } = fakeApi(() => view());
    expect(FOLLOW_GRACE_MS).toBe(60_000);
    const result = await new StepTestClient(api, clock).follow(
      ID,
      10,
      () => undefined,
      new AbortController().signal,
    );
    expect(result).toEqual({ stopped: "timeout" });
    // 30 reads a second apart (0 to 29 s), then 20 two seconds apart (30 to 68 s).
    expect(clock.now()).toBe(70_000);
    expect(calls.length).toBe(30 + 20);
  });

  it("backs off after read errors and gives up after three", async () => {
    const { clock, waits } = fakeClock();
    const { api } = fakeApi((_call, index) =>
      index === 0 ? view() : problem(503, "WV-UNAVAILABLE"),
    );
    const result = await new StepTestClient(api, clock).follow(
      ID,
      300,
      () => undefined,
      new AbortController().signal,
    );
    expect(result).toEqual({ stopped: "lost" });
    expect(waits).toEqual([1000, 2000, 4000]);
  });

  it("a read error in between doesn't count once a read works again", async () => {
    const { clock, waits } = fakeClock();
    const answers = [view(), problem(503, "X"), problem(503, "X"), view()];
    const { api } = fakeApi(
      (_call, index) => answers[index] ?? view({ status: "failed" }),
    );
    const result = await new StepTestClient(api, clock).follow(
      ID,
      300,
      () => undefined,
      new AbortController().signal,
    );
    expect(result).toEqual(view({ status: "failed" }));
    expect(waits).toEqual([1000, 2000, 4000, 1000]);
  });

  it("stops at once when asked to", async () => {
    const { clock } = fakeClock();
    const controller = new AbortController();
    const { api, calls } = fakeApi(() => {
      controller.abort();
      return view();
    });
    const seen: StepTestView[] = [];
    expect(
      await new StepTestClient(api, clock).follow(
        ID,
        300,
        (v) => seen.push(v),
        controller.signal,
      ),
    ).toEqual({ stopped: "aborted" });
    expect(calls).toHaveLength(1);
    expect(seen).toEqual([]);
  });

  it("Stop cancels on the platform and reads the ended view", async () => {
    const { api, calls } = fakeApi(() => view({ status: "cancelled" }));
    expect(await new StepTestClient(api, fakeClock().clock).cancel(ID)).toEqual(
      view({ status: "cancelled" }),
    );
    expect(calls.map((c) => [c.path, c.method])).toEqual([
      [`${ENV}/step-tests/${ID}/cancel`, "POST"],
    ]);
    const failing = fakeApi(() => problem(404, "WV-NOT-FOUND"));
    expect(
      await new StepTestClient(failing.api, fakeClock().clock).cancel(ID),
    ).toBeNull();
  });

  it("maps each refusal", () => {
    expect(
      refusalOf(
        problem(409, "WV-STEPTEST-CONFIRM", {
          side_effect: "non_idempotent",
          action: "acme.send@1.0.0",
          environment: "local",
        }),
      ),
    ).toEqual({
      kind: "confirm",
      sideEffect: "non_idempotent",
      action: "acme.send@1.0.0",
      environment: "local",
    });
    expect(
      refusalOf(
        problem(422, "WV-STEPTEST-RELEASE", {
          choices: { "acme.send@1.0.0": [{ id: "r1", label: "acme 1.0.0" }] },
        }),
      ),
    ).toEqual({
      kind: "release",
      choices: { "acme.send@1.0.0": [{ id: "r1", label: "acme 1.0.0" }] },
      message: "WV-STEPTEST-RELEASE said no.",
    });
    expect(
      refusalOf(
        problem(422, "WV-STEPTEST-INPUT", {
          diagnostics: [{ path: "/to", code: "required" }],
        }),
      ),
    ).toEqual({
      kind: "input",
      diagnostics: [{ path: "/to", code: "required" }],
    });
    expect(
      refusalOf(problem(422, "WV-SCHEMA-SECRET_VALUE", { paths: ["/key"] })),
    ).toEqual({ kind: "secret", paths: ["/key"] });
    expect(
      refusalOf(
        problem(422, "WV-STEPTEST-CONNECTION", {
          slots: { crm: "Choose a connection for slot crm." },
        }),
      ),
    ).toEqual({
      kind: "connection",
      slots: { crm: "Choose a connection for slot crm." },
    });
    expect(refusalOf(problem(403, "WV-STEPTEST-DISABLED"))).toEqual({
      kind: "disabled",
    });
    expect(refusalOf(problem(403, "WV-FORBIDDEN"))).toEqual({
      kind: "forbidden",
    });
    expect(refusalOf(problem(404, "WV-NOT-FOUND"))).toEqual({
      kind: "not-found",
    });
    expect(refusalOf(problem(422, "WV-STEPTEST-KIND"))).toEqual({
      kind: "kind",
      message: "WV-STEPTEST-KIND said no.",
    });
    expect(
      refusalOf(new ApiError(429, { code: "WV-STEPTEST-BUSY" }, 3)),
    ).toEqual({ kind: "busy", retryAfterMs: 3000 });
    expect(refusalOf(problem(409, "WV-PRINCIPAL-CHANGED"))).toEqual({
      kind: "account-changed",
    });
    expect(refusalOf(new TypeError("Failed to fetch"))).toEqual({
      kind: "offline",
    });
    expect(refusalOf(problem(409, "WV-STUDIO-OFFLINE"))).toEqual({
      kind: "offline",
    });
    expect(refusalOf(problem(502, "WV-STUDIO-UPSTREAM"))).toEqual({
      kind: "lost",
    });
    expect(
      refusalOf(new DOMException("The request took too long.", "TimeoutError")),
    ).toEqual({ kind: "lost" });
    expect(refusalOf(problem(500, "WV-INTERNAL"))).toEqual({
      kind: "failed",
      message: "WV-INTERNAL said no.",
    });
    // A refusal whose result isn't what the platform documents reads as a failure.
    expect(
      refusalOf(
        problem(409, "WV-STEPTEST-CONFIRM", { side_effect: "sometimes" }),
      ).kind,
    ).toBe("failed");
  });

  it("reads only typed views", () => {
    expect(readStepTestView(view())).toEqual(view());
    expect(readStepTestView({ ...view(), status: "done" })).toBeNull();
    expect(readStepTestView({ ...view(), side_effect: "maybe" })).toBeNull();
    expect(readStepTestView({ ...view(), run_id: 7 })).toBeNull();
    expect(
      readStepTestView({ ...view(), error: { code: "X", message: 1 } }),
    ).toBeNull();
    expect(
      readStepTestView({
        ...view(),
        pending: [{ node_id: "a", instance_key: "", kind: "confirm" }],
      }),
    ).toBeNull();
    expect(
      readStepTestView({
        ...view(),
        output: { secret: "x" },
        omitted: "classified_secret",
      }),
    ).toBeNull();
    expect(
      readStepTestView({
        ...view(),
        status: "succeeded",
        omitted: "classified_secret",
      }),
    ).toEqual(view({ status: "succeeded", omitted: "classified_secret" }));
    expect(readStepTestView("x")).toBeNull();
  });
});
