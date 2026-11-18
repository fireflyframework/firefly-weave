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
// Listening for a webhook's test event: Studio asks the platform for a test
// URL on a test activation, shows it with a signed cURL, and watches the
// activation's test runs; the first run a signed request starts brings its
// payload back as the workflow's test input. Stop listening retires the
// activation, which retires the test URL with it.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";
import {
  TestEventListener,
  type ListenPort,
  type ReceivedEvent,
} from "../src/app/editor/triggers/test-listener";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const START = Date.parse("2026-11-18T10:00:00Z");
const HOOK: TriggerIntent = {
  id: "trigger-1b2c3d4e",
  kind: "webhook",
  name: "orders-in",
  secretRef: "orders-signing",
  testSecretRef: "orders-test",
};
const LISTENING = {
  listening: {
    test_url: "https://platform.test/hooks/test/t-1",
    expires_at: new Date(START + 15 * 60_000).toISOString(),
  },
  activation: {
    id: "ta-1",
    activation_id: "a-1",
    artifact_digest: "d".repeat(64),
    side_effect: "read_only" as const,
    expires_at: new Date(START + 15 * 60_000).toISOString(),
  },
};
const RUN = "5a4b1dce-8baa-4cb0-8fa6-9e6a5b7c2d56";
const runView = (input: unknown) => ({
  id: RUN,
  activation: {
    id: "a-1",
    request: { version_id: "d3c29f40-0322-4e38-8b2e-16e2d3f4afde" },
  },
  state: {
    status: "running",
    input,
    steps: {},
    waits: {},
    incidents: {},
    branches: {},
    active: [],
  },
});

class Fake implements ListenPort {
  readonly log: string[] = [];
  readonly reads: string[] = [];
  answer: unknown = { ok: true, value: LISTENING };
  summaries: unknown[] = [];
  run: unknown = runView({ orderId: "o-1" });
  failRead = 0;
  clock = START;
  readonly environment = ENV;
  async listen(trigger: TriggerIntent) {
    this.log.push(`listen ${trigger.id}`);
    return this.answer as never;
  }
  async forget() {
    this.log.push("retire");
  }
  async request<T>(path: string): Promise<T> {
    this.reads.push(path.slice(ENV.length));
    if (this.failRead > 0) {
      this.failRead--;
      throw new ApiError(503, { code: "WV-UNAVAILABLE", message: "Down" });
    }
    if (path.includes("/run-summaries")) {
      const next = this.summaries.shift();
      return {
        items: next === undefined ? [] : Array.isArray(next) ? next : [next],
        next_cursor: null,
      } as T;
    }
    return this.run as T;
  }
  now() {
    return this.clock;
  }
  async wait() {
    this.clock += 2_000;
  }
}

function listener(
  fake: Fake,
  received: ReceivedEvent[],
  ownRuns: ReadonlySet<string> = new Set(),
) {
  return new TestEventListener({
    port: fake,
    api: fake,
    now: () => fake.now(),
    wait: () => fake.wait(),
    received: (event) => received.push(event),
    ownRuns: () => ownRuns,
  });
}

describe("listening for a test event", () => {
  it("shows the test URL and a signed cURL, and brings back the payload of the run a signed request starts", async () => {
    const fake = new Fake();
    const received: ReceivedEvent[] = [];
    const listen = listener(fake, received);
    const watching = listen.start(HOOK, {}, { orderId: "sample" });
    await watching.started;
    expect(listen.state()).toMatchObject({
      kind: "listening",
      triggerId: HOOK.id,
      url: "https://platform.test/hooks/test/t-1",
    });
    const state = listen.state();
    expect(state?.kind === "listening" && state.curl).toContain(
      "curl -sS -X POST 'https://platform.test/hooks/test/t-1'",
    );
    expect(state?.kind === "listening" && state.curl).toContain(
      `PAYLOAD='{"orderId":"sample"}'`,
    );
    // It names the webhook's test secret, the one its test URL checks.
    expect(state?.kind === "listening" && state.curl).toContain(
      "with the value of the test secret orders-test first",
    );
    // Nothing yet, then the signed request's run.
    fake.summaries = [undefined, { id: RUN, status: "running" }].filter(
      Boolean,
    );
    await watching.done;
    expect(received).toEqual([
      { triggerId: HOOK.id, runId: RUN, input: { orderId: "o-1" } },
    ]);
    expect(listen.state()).toMatchObject({ kind: "received", runId: RUN });
    const query = new URLSearchParams(fake.reads[0].split("?")[1]);
    expect(fake.reads[0].startsWith("/run-summaries?")).toBe(true);
    expect(Object.fromEntries(query)).toMatchObject({
      activation_id: "a-1",
      include_test: "true",
      // A little before this computer's clock, which may run ahead of the platform's.
      started_after: new Date(START - 120_000).toISOString(),
      limit: "10",
    });
    expect(fake.reads.at(-1)).toBe(`/runs/${RUN}`);
  });

  it("takes only a run the test URL started: not one Execute started, nor one it took before", async () => {
    const fake = new Fake();
    const received: ReceivedEvent[] = [];
    const EXECUTED = "11111111-2222-4333-8444-555555555555";
    const listen = listener(fake, received, new Set([EXECUTED]));
    fake.summaries = [[{ id: EXECUTED }, { id: RUN }]];
    await listen.start(HOOK, {}).done;
    expect(received.map((event) => event.runId)).toEqual([RUN]);
    // Listening again, the run already taken is not a new test event.
    fake.summaries = [[{ id: RUN }], [{ id: RUN }]];
    const again = listen.start(HOOK, {});
    await again.started;
    fake.clock = START + 15 * 60_000;
    await again.done;
    expect(received.map((event) => event.runId)).toEqual([RUN]);
  });

  it("keeps watching past a failed read, and gives up after three in a row", async () => {
    const fake = new Fake();
    const received: ReceivedEvent[] = [];
    const listen = listener(fake, received);
    fake.failRead = 3;
    const watching = listen.start(HOOK, {});
    await watching.done;
    expect(received).toEqual([]);
    expect(listen.state()).toEqual({
      kind: "failed",
      triggerId: HOOK.id,
      message: "Studio stopped listening: Down",
    });
  });

  it("says so when the platform's answer has no test URL", async () => {
    const fake = new Fake();
    fake.answer = {
      ok: true,
      value: {
        ...LISTENING,
        listening: { expires_at: LISTENING.listening.expires_at },
      },
    };
    const listen = listener(fake, []);
    await listen.start(HOOK, {}).done;
    expect(listen.state()).toEqual({
      kind: "failed",
      triggerId: HOOK.id,
      message: "The platform didn't answer with a test URL. Listen again.",
    });
  });

  it("stops when the test URL expires", async () => {
    const fake = new Fake();
    const listen = listener(fake, []);
    const watching = listen.start(HOOK, {});
    await watching.started;
    fake.clock = START + 15 * 60_000;
    await watching.done;
    expect(listen.state()).toEqual({
      kind: "failed",
      triggerId: HOOK.id,
      message: "The test URL expired. Listen again to get a new one.",
    });
  });

  it("ends with a reason when its test URL was retired for it, retiring nothing more", async () => {
    const fake = new Fake();
    const listen = listener(fake, []);
    const watching = listen.start(HOOK, {});
    await watching.started;
    listen.ended("The workflow changed, so Studio stopped listening.");
    await watching.done;
    expect(listen.state()).toEqual({
      kind: "failed",
      triggerId: HOOK.id,
      message: "The workflow changed, so Studio stopped listening.",
    });
    expect(fake.log).toEqual([`listen ${HOOK.id}`]);
  });

  it("says why it can't listen, and Stop listening retires the test URL", async () => {
    const fake = new Fake();
    fake.answer = {
      ok: false,
      refusal: {
        kind: "no-secret",
        message: "Choose the webhook's test secret first.",
      },
    };
    const listen = listener(fake, []);
    await listen.start(HOOK, {}).done;
    expect(listen.state()).toEqual({
      kind: "failed",
      triggerId: HOOK.id,
      message: "Choose the webhook's test secret first.",
    });

    fake.answer = { ok: true, value: LISTENING };
    const watching = listen.start(HOOK, {});
    await watching.started;
    await listen.stop();
    await watching.done;
    expect(listen.state()).toBeNull();
    expect(fake.log).toEqual([
      `listen ${HOOK.id}`,
      `listen ${HOOK.id}`,
      "retire",
    ]);
  });
});
