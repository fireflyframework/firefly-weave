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
// Executing the workflow in an environment from the editor: Studio saves the
// draft, keeps one test activation per environment, draft revision and
// bindings while it lasts, asks before running steps that change data, and
// retires the activation when the editor closes or the draft changes.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { TestActivationApi } from "../src/app/editor/state/test-activation-client";
import {
  TestActivationService,
  type TestActivationHost,
} from "../src/app/editor/state/test-activation-service";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const EXPIRES = Date.parse("2026-11-18T10:15:00Z");

class FakeApi implements TestActivationApi {
  readonly environment = ENV;
  readonly calls: { method: string; path: string; body: unknown }[] = [];
  answers: unknown[] = [];
  constructor(private readonly log: string[]) {}
  async mutate<T>(path: string, method: string, body: unknown) {
    this.calls.push({ method, path: path.slice(ENV.length), body });
    this.log.push(`${method} ${path.slice(ENV.length)}`);
    const answer = this.answers.shift();
    if (answer instanceof Error) throw answer;
    return answer as T;
  }
}

class FakeHost implements TestActivationHost {
  clock = EXPIRES - 10 * 60_000;
  revision = 4;
  environmentChoice: { id: string; name: string } | null = {
    id: "e",
    name: "local",
  };
  confirmation = true;
  readonly asked: unknown[] = [];
  constructor(private readonly log: string[]) {}
  environment() {
    return this.environmentChoice;
  }
  async saveDraft() {
    this.log.push("save");
    return { draftId: "draft-1", draftRevision: this.revision };
  }
  async confirm(request: unknown) {
    this.asked.push(request);
    return this.confirmation;
  }
  now() {
    return this.clock;
  }
}

const activation = (id: string, expires = EXPIRES) => ({
  id,
  activation_id: `a-${id}`,
  artifact_digest: "d".repeat(64),
  side_effect: "read_only",
  expires_at: new Date(expires).toISOString(),
});
const RUN = { id: "run-1", status: "running" };
const BINDINGS = { connection_revision_ids: { crm: "r-1", erp: "r-2" } };

function setup() {
  const log: string[] = [];
  const api = new FakeApi(log);
  const host = new FakeHost(log);
  return { log, api, host, service: new TestActivationService(api, host) };
}

describe("executing in an environment", () => {
  it("saves the draft first, then creates a test activation and starts a run", async () => {
    const { log, api, service } = setup();
    api.answers = [activation("ta-1"), RUN];
    const outcome = await service.execute({ region: "eu" }, BINDINGS);
    expect(outcome).toEqual({
      ok: true,
      value: { run: RUN, activation: activation("ta-1") },
    });
    expect(log).toEqual([
      "save",
      "POST /test-activations",
      "POST /test-activations/ta-1/runs",
    ]);
    expect(api.calls[0].body).toEqual({
      draft_id: "draft-1",
      draft_revision: 4,
      bindings: BINDINGS,
    });
    expect(api.calls[1].body).toEqual({ input: { region: "eu" } });
  });

  it("reuses the activation for the same draft revision and bindings until a minute before it expires", async () => {
    const { log, api, host, service } = setup();
    api.answers = [activation("ta-1"), RUN, RUN, {}, activation("ta-2"), RUN];
    await service.execute({}, BINDINGS);
    host.clock = EXPIRES - 61_000;
    await service.execute(
      {},
      { connection_revision_ids: { erp: "r-2", crm: "r-1" } },
    );
    host.clock = EXPIRES - 59_000;
    await service.execute({}, BINDINGS);
    expect(log.filter((entry) => entry !== "save")).toEqual([
      "POST /test-activations",
      "POST /test-activations/ta-1/runs",
      "POST /test-activations/ta-1/runs",
      "DELETE /test-activations/ta-1",
      "POST /test-activations",
      "POST /test-activations/ta-2/runs",
    ]);
  });

  it.each([
    ["a new draft revision", (host: FakeHost) => (host.revision = 5), BINDINGS],
    [
      "other bindings",
      () => undefined,
      { connection_revision_ids: { crm: "r-9" } },
    ],
    [
      "another environment",
      (host: FakeHost) =>
        (host.environmentChoice = { id: "e2", name: "staging" }),
      BINDINGS,
    ],
  ])(
    "starts a new activation for %s and retires the old one",
    async (_, change, bindings) => {
      const { log, api, host, service } = setup();
      api.answers = [activation("ta-1"), RUN, {}, activation("ta-2"), RUN];
      await service.execute({}, BINDINGS);
      change(host);
      await service.execute({}, bindings);
      expect(
        log.filter(
          (entry) =>
            entry.startsWith("POST /test-activations") ||
            entry.startsWith("DELETE"),
        ),
      ).toEqual([
        "POST /test-activations",
        "POST /test-activations/ta-1/runs",
        "DELETE /test-activations/ta-1",
        "POST /test-activations",
        "POST /test-activations/ta-2/runs",
      ]);
    },
  );

  it("creates one activation when two executions start together", async () => {
    const { api, service } = setup();
    api.answers = [activation("ta-1"), RUN, { ...RUN, id: "run-2" }];
    const [first, second] = await Promise.all([
      service.execute({}, BINDINGS),
      service.execute({}, BINDINGS),
    ]);
    expect([first.ok, second.ok]).toEqual([true, true]);
    expect(
      api.calls.filter((call) => call.path === "/test-activations"),
    ).toHaveLength(1);
  });
});

describe("confirming steps that change data", () => {
  const CONFIRM = () =>
    new ApiError(409, {
      code: "WV-STEPTEST-CONFIRM",
      message:
        "This workflow changes data; confirm its effect to run it for real",
      result: {
        side_effect: "non_idempotent",
        steps: ["charge-card", "send-invoice"],
      },
    });

  it("asks once and creates the activation again with the acknowledgement", async () => {
    const { api, host, service } = setup();
    api.answers = [CONFIRM(), activation("ta-1"), RUN];
    expect((await service.execute({}, BINDINGS)).ok).toBe(true);
    expect(host.asked).toEqual([
      { sideEffect: "non_idempotent", steps: ["charge-card", "send-invoice"] },
    ]);
    expect(api.calls[1].body).toEqual({
      draft_id: "draft-1",
      draft_revision: 4,
      bindings: BINDINGS,
      acknowledged_side_effect: "non_idempotent",
    });
  });

  it("runs nothing when the person cancels", async () => {
    const { api, host, service } = setup();
    host.confirmation = false;
    api.answers = [CONFIRM()];
    expect(await service.execute({}, BINDINGS)).toEqual({
      ok: false,
      refusal: { kind: "cancelled", message: "Nothing ran." },
    });
    expect(api.calls).toHaveLength(1);
  });

  it("doesn't ask twice", async () => {
    const { api, host, service } = setup();
    api.answers = [CONFIRM(), CONFIRM()];
    expect(await service.execute({}, BINDINGS)).toMatchObject({
      ok: false,
      refusal: { kind: "confirm" },
    });
    expect([host.asked.length, api.calls.length]).toEqual([1, 2]);
  });
});

describe("what a refusal says", () => {
  it("says an expired activation expired, and the next execution starts a new one", async () => {
    const { api, service } = setup();
    api.answers = [
      activation("ta-1"),
      new ApiError(410, {
        code: "WV-TESTACTIVATION-EXPIRED",
        message: "This test activation has expired",
      }),
      activation("ta-2"),
      RUN,
    ];
    expect(await service.execute({}, BINDINGS)).toEqual({
      ok: false,
      refusal: {
        kind: "expired",
        message:
          "The test activation expired. Execute again to start a new one.",
      },
    });
    expect((await service.execute({}, BINDINGS)).ok).toBe(true);
    expect(api.calls.map((call) => call.path)).toEqual([
      "/test-activations",
      "/test-activations/ta-1/runs",
      "/test-activations",
      "/test-activations/ta-2/runs",
    ]);
  });

  it("names the environment when test calls are off", async () => {
    const { api, service } = setup();
    api.answers = [
      new ApiError(403, {
        code: "WV-STEPTEST-DISABLED",
        message: "Test calls are off in this environment",
      }),
    ];
    expect(await service.execute({}, BINDINGS)).toEqual({
      ok: false,
      refusal: {
        kind: "disabled",
        message:
          "Test calls are off in local. An administrator can turn them on in Settings.",
      },
    });
  });

  it("needs an environment, and saves nothing without one", async () => {
    const { log, host, service } = setup();
    host.environmentChoice = null;
    expect(await service.execute({}, BINDINGS)).toMatchObject({
      ok: false,
      refusal: { kind: "no-environment" },
    });
    expect(log).toEqual([]);
  });
});

describe("listening for a test event", () => {
  const HOOK = {
    id: "t-1",
    kind: "webhook" as const,
    name: "orders-in",
    secretRef: "orders-signing",
    maxBodyBytes: 65536,
  };

  it("listens on the activation for a webhook, with its secret and limits", async () => {
    const { api, service } = setup();
    const listening = {
      test_url: "https://weave.example/webhooks/test/x",
      expires_at: "2026-11-18T10:15:00Z",
    };
    api.answers = [activation("ta-1"), listening];
    expect(await service.listen(HOOK, BINDINGS)).toEqual({
      ok: true,
      value: { listening, activation: activation("ta-1") },
    });
    expect(api.calls[1]).toEqual({
      method: "POST",
      path: "/test-activations/ta-1/listen",
      body: { secret_ref: "orders-signing", max_body_bytes: 65536 },
    });
  });

  it.each([
    [{ id: "t-2", kind: "broker" as const }, "Paste a sample message instead."],
    [{ id: "t-3", kind: "email" as const }, "Paste a sample email instead."],
    [{ id: "t-4", kind: "provider" as const }, "Paste a sample event instead."],
    [
      { id: "t-5", kind: "schedule" as const },
      "Only a webhook listens for a test event.",
    ],
  ])("doesn't listen for %j", async (trigger, message) => {
    const { log, service } = setup();
    expect(await service.listen(trigger, BINDINGS)).toEqual({
      ok: false,
      refusal: { kind: "not-webhook", message },
    });
    expect(log).toEqual([]);
  });

  it("asks for the webhook's secret first, and for a platform", async () => {
    const { log, host, service } = setup();
    expect(
      await service.listen({ ...HOOK, secretRef: undefined }, BINDINGS),
    ).toEqual({
      ok: false,
      refusal: {
        kind: "no-secret",
        message: "Choose the webhook's secret first.",
      },
    });
    host.environmentChoice = null;
    expect(await service.listen(HOOK, BINDINGS)).toEqual({
      ok: false,
      refusal: {
        kind: "no-environment",
        message: "Listening needs a platform. Paste a sample payload instead.",
      },
    });
    expect(log).toEqual([]);
  });
});

describe("retiring", () => {
  it("retires the activation when the editor closes or the draft changes, once", async () => {
    const { api, service } = setup();
    api.answers = [activation("ta-1"), RUN, {}, activation("ta-2"), RUN, {}];
    await service.execute({}, BINDINGS);
    await service.draftChanged();
    await service.draftChanged();
    await service.execute({}, BINDINGS);
    await service.forget();
    await service.forget();
    expect(
      api.calls
        .filter((call) => call.method === "DELETE")
        .map((call) => call.path),
    ).toEqual(["/test-activations/ta-1", "/test-activations/ta-2"]);
  });

  it("never throws while retiring", async () => {
    const { api, service } = setup();
    api.answers = [activation("ta-1"), RUN, new TypeError("offline")];
    await service.execute({}, BINDINGS);
    await expect(service.forget()).resolves.toBeUndefined();
  });
});
