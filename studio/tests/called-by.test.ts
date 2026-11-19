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
// The published workflows that call this one, read from the project's
// catalog lock: each calling workflow's newest version that calls it, the
// step that calls, whether it waits for the result (or calls it as an AI
// agent's tool), and whether this workflow still allows it.
import { describe, expect, it } from "vitest";
import {
  calledBy,
  callerLine,
  callerProblem,
  callersCaption,
  removalQuestion,
  withCaller,
  withoutCaller,
  type Caller,
} from "../src/app/editor/calls/called-by";
import { parseCatalogLock } from "../src/app/editor/calls/catalog-lock";

const workflow = (name: string, version: string, steps: unknown[]) => ({
  document: {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name, version },
    spec: { steps },
  },
  digest: "0".repeat(64),
});
const call = (id: string, uses: string, mode = "wait") => ({
  id,
  kind: "callWorkflow",
  uses,
  mode,
  with: { literal: {} },
});
const lock = parseCatalogLock({
  definitions: [
    workflow("order-intake", "1.2.0", [
      call("notify", "notify-customer@1.0.0"),
    ]),
    workflow("order-intake", "1.3.0", []),
    workflow("order-intake", "1.1.0", [
      call("notify", "notify-customer@0.9.0"),
    ]),
    workflow("billing-close", "2.0.0", [
      {
        id: "route",
        kind: "switch",
        cases: [
          {
            when: { literal: true },
            steps: [call("notify-owner", "notify-customer@1.0.0", "detach")],
            output: { literal: {} },
          },
        ],
        default: { steps: [], output: { literal: {} } },
      },
    ]),
    workflow("support-desk", "1.1.0", [
      {
        id: "assistant",
        kind: "agent",
        tools: [{ name: "notify", workflow: "notify-customer@1.0.0" }],
      },
    ]),
    workflow("returns-desk", "1.0.0", [
      call("tell-customer", "notify-customer@1.0.0"),
    ]),
    workflow("unrelated", "1.0.0", [call("x", "other@1.0.0")]),
  ],
})!;

describe("the workflows that call this one", () => {
  it("lists each published caller with its step and whether it waits, those not allowed last", () => {
    expect(
      calledBy({
        workflow: "notify-customer",
        callable: {
          allowedCallers: ["order-intake", "billing-close", "support-desk"],
        },
        lock,
      }),
    ).toEqual([
      {
        name: "billing-close",
        version: "2.0.0",
        step: "notify-owner",
        mode: "doesn't wait",
        allowed: true,
      },
      {
        name: "order-intake",
        version: "1.2.0",
        step: "notify",
        mode: "waits",
        allowed: true,
      },
      {
        name: "support-desk",
        version: "1.1.0",
        step: "assistant",
        mode: "tool",
        allowed: true,
      },
      {
        name: "returns-desk",
        version: "1.0.0",
        step: "tell-customer",
        mode: "waits",
        allowed: false,
      },
    ]);
  });

  it("allows every caller of a workflow open to any workflow, and none without callable", () => {
    const open = calledBy({ workflow: "notify-customer", callable: {}, lock });
    expect(open.every((row) => row.allowed)).toBe(true);
    expect(open).toHaveLength(4);
    const closed = calledBy({
      workflow: "notify-customer",
      callable: undefined,
      lock,
    });
    expect(closed.every((row) => !row.allowed)).toBe(true);
  });

  it("counts a step in a transaction undone by calling this workflow as waiting", () => {
    const undo = parseCatalogLock({
      definitions: [
        workflow("ledger-post", "1.0.0", [
          {
            id: "booking",
            kind: "transaction",
            steps: [
              {
                // A call that doesn't wait, undone by calling this workflow: the undo still waits.
                id: "reserve",
                kind: "callWorkflow",
                uses: "hold-stock@1.0.0",
                mode: "detach",
                with: { literal: {} },
                compensate: {
                  kind: "callWorkflow",
                  uses: "notify-customer@1.0.0",
                  with: { literal: {} },
                },
              },
            ],
          },
        ]),
      ],
    })!;
    expect(
      calledBy({ workflow: "notify-customer", callable: {}, lock: undo }),
    ).toEqual([
      {
        name: "ledger-post",
        version: "1.0.0",
        step: "reserve",
        mode: "waits",
        allowed: true,
      },
    ]);
  });

  it("finds no caller for a workflow nothing calls", () => {
    expect(calledBy({ workflow: "nobody", callable: {}, lock })).toEqual([]);
  });
});

const caller = (extra: Partial<Caller> = {}): Caller => ({
  name: "order-intake",
  version: "1.2.0",
  step: "notify",
  mode: "waits",
  allowed: true,
  ...extra,
});

describe("what the Called by trigger says about each caller", () => {
  it("says how it calls, shorter on a phone, and why one that isn't allowed fails", () => {
    expect(callerLine(caller(), false)).toBe(
      "Step notify · waits for the result",
    );
    expect(callerLine(caller(), true)).toBe("Step notify · waits");
    expect(callerLine(caller({ mode: "doesn't wait" }), false)).toBe(
      "Step notify · doesn't wait",
    );
    expect(callerLine(caller({ step: "assistant", mode: "tool" }), false)).toBe(
      "AI agent assistant · workflow tool",
    );
    expect(callerLine(caller({ step: "assistant", mode: "tool" }), true)).toBe(
      "AI agent assistant · tool",
    );
    const refused = caller({
      name: "returns-desk",
      step: "tell-customer",
      allowed: false,
    });
    expect(callerLine(refused, false)).toBe(
      "Step tell-customer calls this workflow, but returns-desk isn't one of the workflows allowed. Its next activation will fail.",
    );
    expect(callerLine(refused, true)).toBe(
      "Calls this workflow but isn't allowed. Its next activation will fail.",
    );
  });

  it("counts the published workflows that call it", () => {
    expect(callersCaption(1)).toBe("1 published workflow");
    expect(callersCaption(4)).toBe("4 published workflows");
  });
});

describe("who may call it", () => {
  it("adds a name once, by the platform's name rule, up to 100", () => {
    expect(callerProblem("billing-close", ["order-intake"])).toBeNull();
    expect(callerProblem("order-intake", ["order-intake"])).toBe(
      "order-intake can already call this workflow.",
    );
    expect(callerProblem("-close", [])).toBe(
      "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
    );
    const full = Array.from({ length: 100 }, (_, n) => `w${n}`);
    expect(callerProblem("one-more", full)).toBe(
      "Up to 100 workflows can be named here.",
    );
    expect(withCaller({}, "order-intake")).toEqual({
      allowedCallers: ["order-intake"],
    });
    expect(
      withCaller({ allowedCallers: ["order-intake"] }, "billing-close"),
    ).toEqual({ allowedCallers: ["order-intake", "billing-close"] });
  });

  it("keeps at least one name: removing the last would let any workflow call it", () => {
    expect(withoutCaller({ allowedCallers: ["a", "b"] }, "a")).toEqual({
      allowedCallers: ["b"],
    });
    expect(withoutCaller({ allowedCallers: ["a"] }, "a")).toBeNull();
  });
});

describe("removing the Called by trigger", () => {
  it("names the callers, each with its step, and that the settings' Callable turns off too", () => {
    const question = removalQuestion("notify-customer", [
      caller(),
      caller({ name: "billing-close", version: "2.0.0", step: "notify-owner" }),
      caller({
        name: "support-desk",
        version: "1.1.0",
        step: "assistant",
        mode: "tool",
      }),
    ]);
    expect(question).toMatchObject({
      title: "Remove Called by a workflow?",
      confirmLabel: "Remove",
      danger: true,
      startOnCancel: true,
    });
    expect(question.message.split(/\n\s*\n/)).toEqual([
      "order-intake, billing-close and support-desk call this workflow. Their next activations will fail until they stop calling it.",
      "order-intake 1.2.0 · step notify",
      "billing-close 2.0.0 · step notify-owner",
      "support-desk 1.1.0 · AI agent assistant",
      "Other workflows can no longer call notify-customer from its next version. Callable by other workflows in the workflow settings turns off too.",
    ]);
  });

  it("speaks of one caller in the singular, and of unknown callers in general", () => {
    expect(
      removalQuestion("notify-customer", [caller()]).message.split(
        /\n\s*\n/,
      )[0],
    ).toBe(
      "order-intake calls this workflow. Its next activation will fail until it stops calling it.",
    );
    expect(
      removalQuestion("notify-customer", null).message.split(/\n\s*\n/),
    ).toEqual([
      "Workflows that call this one fail at their next activation until they stop calling it.",
      "Other workflows can no longer call notify-customer from its next version. Callable by other workflows in the workflow settings turns off too.",
    ]);
    expect(
      removalQuestion("notify-customer", []).message.split(/\n\s*\n/),
    ).toEqual([
      "Other workflows can no longer call notify-customer from its next version. Callable by other workflows in the workflow settings turns off too.",
    ]);
  });
});
