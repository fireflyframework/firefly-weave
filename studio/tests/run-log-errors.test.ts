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
// The run log of a simulated run in which a step failed and its On error
// went on: the header says it succeeded with errors, a sentence says how
// many failures were handled, the error path's steps sit under their step,
// and a loop's item that failed and went on says so.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import {
  handledSentence,
  loopLog,
  runStatus,
  stoppedRow,
} from "../src/app/editor/debugger/run-log-panel";
import { errorPathsAround } from "../src/app/editor/state/execute-gate";
import { runFailureOf } from "../src/app/editor/canvas/trace-playback";
import type { Step } from "../src/app/model";
import type { ExecuteResponse } from "../src/app/editor/state/execution";

const error = (instanceKey: string) => ({
  code: "REQUEST_REFUSED",
  message: "The service refused the request.",
  outcome: "failed",
  attempts: 1,
  instanceKey,
});
/** Entries as the Studio host answers them: [step, instance key, kind, status]. */
const answer = (
  entries: [string, string, string, string][],
  handled: string[],
): ExecuteResponse =>
  ({
    status: "completed",
    trace: entries.map(([nodeId, instanceKey, kind, status], index) => ({
      index,
      nodeId,
      instanceKey,
      kind,
      status,
      ...(handled.includes(instanceKey || nodeId)
        ? { error: error(instanceKey || nodeId), handled: "errorOutput" }
        : {}),
      ...(kind === "forEach" ? { output: [1, 2, 3] } : {}),
    })),
    loops: [],
    blocked: null,
  }) as unknown as ExecuteResponse;
const HANDLED = answer(
  [
    ["submit", "", "action", "failed"],
    ["notify-team", "", "action", "completed"],
    ["flag-request", "", "transform", "completed"],
    ["summarize", "", "transform", "completed"],
  ],
  ["submit"],
);

describe("the run log of a run that handled a failure", () => {
  it("says the run succeeded with errors, counting the step that failed and went on", () => {
    expect(runStatus(HANDLED, null)).toBe("Succeeded with errors · 4 steps");
    // Without a handled failure, as before.
    const plain = answer([["summarize", "", "transform", "completed"]], []);
    expect(runStatus(plain, null)).toBe("Completed · 1 step");
  });

  it("says how many failures the run handled, first", () => {
    expect(handledSentence(HANDLED)).toBe(
      "The simulated run succeeded with 1 handled error.",
    );
    const twice = answer(
      [
        ["submit", "", "action", "failed"],
        ["lookup", "", "action", "failed"],
      ],
      ["submit", "lookup"],
    );
    expect(handledSentence(twice)).toBe(
      "The simulated run succeeded with 2 handled errors.",
    );
    expect(
      handledSentence(answer([["submit", "", "action", "completed"]], [])),
    ).toBeNull();
    // A run that failed later doesn't say it succeeded.
    expect(handledSentence({ ...HANDLED, status: "failed" })).toBeNull();
  });

  it("knows the error paths around each step, outermost first", () => {
    const step = (id: string, extra: object = {}) =>
      ({ id, kind: "transform", ...extra }) as Step;
    const around = errorPathsAround([
      step("submit", {
        onError: "errorOutput",
        errorPath: {
          steps: [
            step("notify-team", {
              onError: "errorOutput",
              errorPath: { steps: [step("page-ops")], output: null },
            }),
            {
              id: "route",
              kind: "switch",
              cases: [{ when: true, steps: [step("flag-request")] }],
              default: { steps: [] },
            } as unknown as Step,
          ],
          output: null,
        },
      }),
      step("summarize"),
    ]);
    expect(around("notify-team")).toEqual(["submit"]);
    expect(around("page-ops")).toEqual(["submit", "notify-team"]);
    // A decision inside the path keeps its steps in it.
    expect(around("flag-request")).toEqual(["submit"]);
    expect(around("submit")).toEqual([]);
    expect(around("summarize")).toEqual([]);
  });

  it("says a loop's item failed and went on, with its code, and finishes every item", () => {
    const looped = answer(
      [
        ["orders", "", "forEach", "completed"],
        ["send", "send[0]", "action", "completed"],
        ["send", "send[1]", "action", "failed"],
        ["send", "send[2]", "action", "completed"],
      ],
      ["send[1]"],
    );
    const [orders] = loopLog(looped, (id) => (id === "send" ? ["orders"] : []));
    expect(orders.items).toEqual([
      { label: "Item 1", status: "completed", text: "send", key: "send[0]" },
      {
        label: "Item 2",
        status: "handled",
        text: "Failed, continued at send",
        code: "REQUEST_REFUSED",
        key: "send[1]",
      },
      { label: "Item 3", status: "completed", text: "send", key: "send[2]" },
    ]);
    expect(orders.summary).toBe("3 items");
  });
});

describe("a run that handled one failure, then stopped at another", () => {
  // submit failed and went on; summarize then failed and stopped the run.
  const stopped = {
    ...answer(
      [
        ["submit", "", "action", "failed"],
        ["summarize", "", "transform", "failed"],
      ],
      ["submit"],
    ),
    status: "failed",
    diagnostics: [
      { code: "WV-EXPR-MISSING", message: "A value is missing.", path: "" },
    ],
  } as unknown as ExecuteResponse;

  it("names the step that stopped it, not the one that went on", () => {
    expect(runStatus(stopped, null)).toBe("Failed at summarize");
    expect(runFailureOf(stopped, "summarize", undefined)).toEqual({
      code: "WV-EXPR-MISSING",
      message: "A value is missing.",
    });
    expect(runFailureOf(stopped, "submit", undefined)).toBeNull();
  });

  it("gives the stopping failure's code to the item it stopped in, past an item that went on", () => {
    const looped = {
      ...answer(
        [
          ["orders", "", "forEach", "running"],
          ["send", "send[0]", "action", "failed"],
          ["send", "send[1]", "action", "failed"],
        ],
        ["send[0]"],
      ),
      status: "failed",
      diagnostics: [{ code: "SMTP_REJECTED", message: "Rejected.", path: "" }],
    } as unknown as ExecuteResponse;
    const [orders] = loopLog(looped, (id) => (id === "send" ? ["orders"] : []));
    expect(orders.items[0]).toMatchObject({ status: "handled" });
    expect(orders.items[1]).toMatchObject({
      status: "failed",
      text: "Failed at send",
      message: "Rejected.",
      steps: [{ id: "send", status: "failed", text: "SMTP_REJECTED" }],
    });
  });

  it("names the step that stopped it past a called run's failure its call handled", () => {
    // As the Studio host answers it: notify's called run failed at send,
    // notify's On error went on, then stop-here stopped the run.
    const called = {
      status: "failed",
      trace: [
        {
          index: 0,
          nodeId: "notify",
          instanceKey: "",
          kind: "callWorkflow",
          status: "failed",
          error: { ...error("notify"), code: "SMTP_REJECTED" },
          handled: "continue",
        },
        {
          index: 1,
          nodeId: "send",
          instanceKey: "",
          kind: "fail",
          status: "failed",
          frame: "notify",
        },
        {
          index: 2,
          nodeId: "stop-here",
          instanceKey: "",
          kind: "fail",
          status: "failed",
        },
      ],
      loops: [],
      blocked: null,
      diagnostics: [],
    } as unknown as ExecuteResponse;
    expect(runStatus(called, null)).toBe("Failed at stop-here");
    expect(runFailureOf(called, "stop-here", undefined)).toEqual({});
    expect(runFailureOf(called, "send", undefined)).toBeNull();
  });
});

describe("the row of the step a run stopped at", () => {
  // submit failed with an unknown outcome; it changes data, so the run stopped with an incident.
  const unknown = {
    status: "failed",
    trace: [
      {
        index: 0,
        nodeId: "submit",
        instanceKey: "",
        kind: "action",
        status: "failed",
        error: { ...error("submit"), outcome: "unknown" },
      },
    ],
    loops: [],
    blocked: null,
    diagnostics: [
      {
        code: "WV-TASK-AMBIGUOUS",
        message: "Whether it applied is unknown.",
        path: "",
      },
    ],
  } as unknown as ExecuteResponse;
  const workflow = (onError?: string) => ({
    spec: {
      steps: [
        {
          id: "submit",
          kind: "action",
          uses: "requests.submit@1.0.0",
          ...(onError ? { onError } : {}),
        },
      ],
    },
  });

  it("gives its code and says why its On error didn't apply when the outcome is unknown", () => {
    expect(stoppedRow(unknown, workflow("continue"))).toEqual({
      step: "submit",
      code: "WV-TASK-AMBIGUOUS",
      reason:
        "Its outcome is unknown and it changes data, so the run stopped with an incident, whatever On error says.",
    });
  });

  it("gives its code alone when its On error stops the run anyway", () => {
    expect(stoppedRow(unknown, workflow())).toEqual({
      step: "submit",
      code: "WV-TASK-AMBIGUOUS",
      reason: null,
    });
  });

  it("gives the step's own code when the answer has no incident", () => {
    const plain = {
      ...unknown,
      trace: [{ ...unknown.trace[0], error: error("submit") }],
      diagnostics: [],
    } as unknown as ExecuteResponse;
    expect(stoppedRow(plain, workflow())).toEqual({
      step: "submit",
      code: "REQUEST_REFUSED",
      reason: null,
    });
    expect(stoppedRow(HANDLED, workflow("errorOutput"))).toBeNull();
  });
});
