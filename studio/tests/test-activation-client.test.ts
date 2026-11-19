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
// The test activations client: create a test activation of the saved draft,
// start runs on it, listen for a test event, retire it, and the platform's
// refusals as typed outcomes.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  createTestActivation,
  listenForTestEvent,
  retireTestActivation,
  runTestActivation,
  type TestActivationApi,
} from "../src/app/editor/state/test-activation-client";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";

class FakeApi implements TestActivationApi {
  readonly environment = ENV;
  readonly calls: {
    path: string;
    method: string;
    body: unknown;
    key?: string;
  }[] = [];
  answers: unknown[] = [];
  async mutate<T>(
    path: string,
    method: string,
    body: unknown,
    _revision?: number,
    key?: string,
  ) {
    this.calls.push({ path, method, body, ...(key ? { key } : {}) });
    const answer = this.answers.shift();
    if (answer instanceof Error) throw answer;
    return answer as T;
  }
}

const ACTIVATION = {
  id: "ta-1",
  activation_id: "a-1",
  artifact_digest: "d".repeat(64),
  side_effect: "read_only",
  expires_at: "2026-11-18T10:15:00Z",
};

describe("the calls", () => {
  it("creates a test activation of the saved draft", async () => {
    const api = new FakeApi();
    api.answers = [ACTIVATION];
    const request = {
      draft_id: "draft-1",
      draft_revision: 4,
      bindings: { connection_revision_ids: { crm: "r-1" } },
    };
    expect(await createTestActivation(api, request)).toEqual({
      ok: true,
      value: ACTIVATION,
    });
    expect(api.calls).toEqual([
      { path: `${ENV}/test-activations`, method: "POST", body: request },
    ]);
  });

  it("starts a run with the input, under the key it is given", async () => {
    const api = new FakeApi();
    api.answers = [{ id: "run-1", status: "running" }];
    expect(
      await runTestActivation(api, "ta/1", { region: "eu" }, "key-1"),
    ).toEqual({
      ok: true,
      value: { id: "run-1", status: "running" },
    });
    expect(api.calls).toEqual([
      {
        path: `${ENV}/test-activations/ta%2F1/runs`,
        method: "POST",
        body: { input: { region: "eu" } },
        key: "key-1",
      },
    ]);
  });

  it("listens for a test event and retires the activation", async () => {
    const api = new FakeApi();
    api.answers = [
      {
        test_url: "https://weave.example/webhooks/test/x",
        expires_at: "2026-11-18T10:15:00Z",
      },
      {},
    ];
    const listen = {
      secret_ref: "orders-signing",
      max_body_bytes: 65536,
      tolerance_seconds: 300,
    };
    expect(await listenForTestEvent(api, "ta-1", listen)).toMatchObject({
      ok: true,
      value: { test_url: "https://weave.example/webhooks/test/x" },
    });
    expect(await retireTestActivation(api, "ta-1")).toEqual({
      ok: true,
      value: null,
    });
    expect(
      api.calls.map((call) => [call.method, call.path, call.body]),
    ).toEqual([
      ["POST", `${ENV}/test-activations/ta-1/listen`, listen],
      ["DELETE", `${ENV}/test-activations/ta-1`, undefined],
    ]);
  });
});

describe("the refusals", () => {
  const refused = async (error: Error) => {
    const api = new FakeApi();
    api.answers = [error];
    return createTestActivation(api, {
      draft_id: "draft-1",
      draft_revision: 1,
    });
  };

  it.each([
    [
      new ApiError(403, {
        code: "WV-STEPTEST-DISABLED",
        message: "Test calls are off in this environment",
      }),
      { kind: "disabled" },
    ],
    [
      new ApiError(409, {
        code: "WV-STEPTEST-CONFIRM",
        message:
          "This workflow changes data; confirm its effect to run it for real",
        result: {
          side_effect: "non_idempotent",
          steps: ["charge-card", "send-invoice"],
        },
      }),
      {
        kind: "confirm",
        sideEffect: "non_idempotent",
        steps: ["charge-card", "send-invoice"],
      },
    ],
    [
      new ApiError(410, {
        code: "WV-TESTACTIVATION-EXPIRED",
        message: "This test activation has expired",
      }),
      { kind: "expired" },
    ],
    [
      new ApiError(422, {
        code: "WV-TESTACTIVATION-COMPILE",
        message: "The saved draft doesn't compile; fix it and try again",
        result: {
          diagnostics: [
            {
              code: "WV-COMP-UNKNOWN_STEP",
              message: "No such step",
              path: "/spec/steps/0",
            },
          ],
        },
      }),
      {
        kind: "compile",
        diagnostics: [
          {
            code: "WV-COMP-UNKNOWN_STEP",
            message: "No such step",
            path: "/spec/steps/0",
          },
        ],
      },
    ],
    [
      new ApiError(422, {
        code: "WV-TESTACTIVATION-LIFETIME",
        message: "At most 3,600 seconds",
      }),
      { kind: "lifetime" },
    ],
    [
      new ApiError(
        429,
        {
          code: "WV-STEPTEST-BUSY",
          message: "You already have 4 test runs running here.",
        },
        12,
      ),
      {
        kind: "busy",
        retryAfter: 12,
        message: "You already have 4 test runs running here.",
      },
    ],
    [
      new ApiError(422, {
        code: "WV-STEPTEST-CONNECTION",
        message: "The connections don't match the draft's slots",
        result: { slots: { crm: "Choose a connection for slot crm." } },
      }),
      {
        kind: "connection",
        slots: { crm: "Choose a connection for slot crm." },
      },
    ],
    [
      new ApiError(403, { code: "WV-FORBIDDEN", message: "Not allowed" }),
      { kind: "forbidden" },
    ],
    [
      new ApiError(404, { code: "WV-NOT-FOUND", message: "Draft not found" }),
      { kind: "missing" },
    ],
    [
      new ApiError(500, { code: "WV-INTERNAL", message: "Broken" }),
      { kind: "failed", status: 500, code: "WV-INTERNAL" },
    ],
  ])("maps %s", async (error, refusal) => {
    expect(await refused(error)).toEqual({
      ok: false,
      refusal: { message: expect.any(String), ...refusal },
    });
  });

  it("keeps a confirmation without steps it can read safe", async () => {
    expect(
      await refused(
        new ApiError(409, {
          code: "WV-STEPTEST-CONFIRM",
          message: "Confirm",
          result: { side_effect: "weird", steps: [3] },
        }),
      ),
    ).toMatchObject({
      ok: false,
      refusal: { kind: "confirm", sideEffect: "non_idempotent", steps: [] },
    });
  });

  it("lets other failures through", async () => {
    await expect(refused(new TypeError("offline"))).rejects.toThrow("offline");
  });
});
