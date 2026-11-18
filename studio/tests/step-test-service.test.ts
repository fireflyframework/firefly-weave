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
import type {
  StepTestRequest,
  StepTestView,
} from "../src/app/editor/state/execution";
import type {
  FollowAnswer,
  StartAnswer,
  StepTestRefusal,
} from "../src/app/editor/state/step-test-client";
import {
  StepTestService,
  type StepTestServiceHost,
} from "../src/app/editor/state/step-test-service";

const ID = "3f2a9c1e-1111-4222-8333-444455556666";
const RUN = "9d8c7b6a-1111-4222-8333-444455556666";
const DRAFT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const view = (over: Partial<StepTestView> = {}): StepTestView => ({
  id: ID,
  run_id: RUN,
  status: "succeeded",
  side_effect: "read_only",
  pending: [],
  output: { ok: 1 },
  error: null,
  attempts: 1,
  started_at: "2026-11-17T10:00:00Z",
  ended_at: "2026-11-17T10:00:02Z",
  duration_ms: 2000,
  ...over,
});
const body = {
  kind: "action",
  step_id: "lookup",
  uses: "acme.lookup@1.0.0",
  input: { id: 1 },
  connections: { crm: "11111111-2222-4333-8444-555555555555" },
  releases: {},
  timeout_seconds: 30,
};

/** A deferred answer the test resolves when it wants. */
function later<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function setup() {
  const state = {
    scope: "account-a",
    environment: { id: "env-local", name: "local" } as {
      id: string;
      name: string;
    } | null,
    draft: { id: DRAFT, revision: 3 } as {
      id: string;
      revision: number;
    } | null,
    steps: new Set(["lookup"]),
    saved: { ok: true, revision: 4 } as
      | { ok: true; revision: number }
      | { ok: false; message: string },
  };
  const posts: { body: StepTestRequest; key: string }[] = [];
  const cancels: string[] = [];
  const recorded: [string, StepTestView][] = [];
  const start = later<StartAnswer>();
  const follow = later<FollowAnswer>();
  let followSignal: AbortSignal | null = null;
  const client = {
    start: (request: StepTestRequest, key: string) => {
      posts.push({ body: request, key });
      return start.promise;
    },
    follow: (
      _id: string,
      _timeout: number,
      _onView: (v: StepTestView) => void,
      signal: AbortSignal,
    ) => {
      followSignal = signal;
      return follow.promise;
    },
    cancel: async (id: string) => {
      cancels.push(id);
      return view({ status: "cancelled", output: null });
    },
  };
  let keys = 0;
  const tracked: (string | null)[] = [];
  const host: StepTestServiceHost = {
    client,
    scope: () => state.scope,
    environment: () => state.environment,
    draft: () => state.draft,
    hasStep: (stepId) => state.steps.has(stepId),
    saveFirst: async () => state.saved,
    record: (stepId, v) => recorded.push([stepId, v]),
    releases: new Map([["acme.lookup@1.0.0", "r-1"]]),
    newKey: () => `key-${++keys}`,
    tracking: (stepId) => tracked.push(stepId),
  };
  const service = new StepTestService(host);
  return {
    state,
    tracked,
    service,
    posts,
    cancels,
    recorded,
    accept() {
      start.resolve({
        ok: true,
        accepted: {
          id: ID,
          run_id: RUN,
          status: "running",
          side_effect: "read_only",
          expires_at: "2026-11-17T10:05:00Z",
        },
      });
    },
    refuse(refusal: StepTestRefusal) {
      start.resolve({ ok: false, refusal });
    },
    finish(answer: FollowAnswer = view()) {
      follow.resolve(answer);
    },
    signal: () => followSignal,
  };
}

describe("the step test service", () => {
  it("saves the draft first and sends its saved revision", async () => {
    const t = setup();
    const outcome = t.service.execute({
      stepId: "lookup",
      body,
      ack: "non_idempotent",
    });
    await tick();
    expect(t.posts).toEqual([
      {
        key: "key-1",
        body: {
          ...body,
          releases: { "acme.lookup@1.0.0": "r-1" },
          acknowledged_side_effect: "non_idempotent",
          draft_id: DRAFT,
          draft_revision: 4,
        },
      },
    ]);
    expect(t.service.running()).toMatchObject({
      stepId: "lookup",
      environment: { id: "env-local", name: "local" },
    });
    t.accept();
    await tick();
    expect(t.service.running()).toMatchObject({ runId: RUN });
    t.finish();
    expect(await outcome).toEqual({ kind: "done", view: view() });
    expect(t.recorded).toEqual([["lookup", view()]]);
    expect(t.service.running()).toBeNull();
  });

  it("a failed save sends nothing", async () => {
    const t = setup();
    t.state.saved = { ok: false, message: "The draft is too large." };
    expect(await t.service.execute({ stepId: "lookup", body })).toEqual({
      kind: "refused",
      refusal: {
        kind: "failed",
        message:
          "Studio couldn't save the draft, so nothing ran: The draft is too large.",
      },
    });
    expect(t.posts).toEqual([]);
    expect(t.service.running()).toBeNull();
  });

  const interleavings: [string, (t: ReturnType<typeof setup>) => void][] = [
    ["switching account", (t) => (t.state.scope = "account-b")],
    [
      "switching environment",
      (t) => (t.state.environment = { id: "env-prod", name: "production" }),
    ],
    [
      "opening another draft",
      (t) =>
        (t.state.draft = {
          id: "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
          revision: 1,
        }),
    ],
    ["removing the step", (t) => t.state.steps.delete("lookup")],
    ["forgetting", (t) => t.service.forget()],
  ];
  for (const [name, change] of interleavings) {
    it(`applies an answer only to the step, draft, account and environment it was asked for: ${name} while starting`, async () => {
      const t = setup();
      const outcome = t.service.execute({ stepId: "lookup", body });
      await tick();
      change(t);
      t.accept();
      expect(await outcome).toEqual({ kind: "stale" });
      expect(t.recorded).toEqual([]);
    });
    it(`applies an answer only to the step, draft, account and environment it was asked for: ${name} while following`, async () => {
      const t = setup();
      const outcome = t.service.execute({ stepId: "lookup", body });
      await tick();
      t.accept();
      await tick();
      change(t);
      t.finish();
      expect(await outcome).toEqual({ kind: "stale" });
      expect(t.recorded).toEqual([]);
    });
  }

  it("applies an answer only to the execution it was asked for: a second execute after forgetting", async () => {
    const t = setup();
    const first = t.service.execute({ stepId: "lookup", body });
    await tick();
    t.service.forget();
    expect(t.signal()).toBeNull();
    const second = t.service.execute({ stepId: "lookup", body });
    await tick();
    t.accept();
    expect(await first).toEqual({ kind: "stale" });
    await tick();
    t.finish();
    expect(await second).toEqual({ kind: "done", view: view() });
    expect(t.recorded).toEqual([["lookup", view()]]);
    expect(t.posts.map((post) => post.key)).toEqual(["key-1", "key-2"]);
  });

  it("forgetting stops following", async () => {
    const t = setup();
    void t.service.execute({ stepId: "lookup", body });
    await tick();
    t.accept();
    await tick();
    expect(t.signal()?.aborted).toBe(false);
    t.service.forget();
    expect(t.signal()?.aborted).toBe(true);
    expect(t.service.running()).toBeNull();
  });

  it("Stop cancels the step test on the platform", async () => {
    const t = setup();
    const outcome = t.service.execute({ stepId: "lookup", body });
    await tick();
    t.accept();
    await tick();
    await t.service.stop();
    expect(t.cancels).toEqual([ID]);
    expect(t.signal()?.aborted).toBe(true);
    t.finish({ stopped: "aborted" });
    const cancelled = view({ status: "cancelled", output: null });
    expect(await outcome).toEqual({ kind: "done", view: cancelled });
    expect(t.recorded).toEqual([["lookup", cancelled]]);
  });

  it("Stop before the platform answered cancels once it does", async () => {
    const t = setup();
    const outcome = t.service.execute({ stepId: "lookup", body });
    await tick();
    const stopping = t.service.stop();
    expect(t.cancels).toEqual([]);
    t.accept();
    await stopping;
    await tick();
    expect(t.cancels).toEqual([ID]);
    t.finish({ stopped: "aborted" });
    expect(await outcome).toMatchObject({ kind: "done" });
  });

  it("one step test at a time per draft", async () => {
    const t = setup();
    void t.service.execute({ stepId: "lookup", body });
    await tick();
    expect(
      await t.service.execute({ stepId: "other", body: { ...body } }),
    ).toEqual({
      kind: "refused",
      refusal: { kind: "busy", retryAfterMs: 0 },
    });
    expect(t.posts).toHaveLength(1);
  });

  it("a refusal from the platform is returned as it is", async () => {
    const t = setup();
    const outcome = t.service.execute({ stepId: "lookup", body });
    await tick();
    t.refuse({ kind: "disabled" });
    expect(await outcome).toEqual({
      kind: "refused",
      refusal: { kind: "disabled" },
    });
    expect(t.recorded).toEqual([]);
    expect(t.service.running()).toBeNull();
  });

  it("a test that outlives the wait says which run to look for", async () => {
    const t = setup();
    const outcome = t.service.execute({ stepId: "lookup", body });
    await tick();
    t.accept();
    await tick();
    t.finish({ stopped: "timeout" });
    expect(await outcome).toEqual({
      kind: "stopped",
      why: "timeout",
      runId: RUN,
    });
    expect(t.recorded).toEqual([]);
  });

  it("tells the host which step it follows, and when it stops", async () => {
    const t = setup();
    void t.service.execute({ stepId: "lookup", body });
    await tick();
    expect(t.tracked).toEqual(["lookup"]);
    // A workspace switch forgets it before any answer comes back.
    t.service.forget();
    expect(t.tracked).toEqual(["lookup", null]);
  });
});
