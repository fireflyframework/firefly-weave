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
// A run that happened and went on after a step failed, as the Runs tab reads
// it: the step's state and its failure from the step facts and the binding
// that holds its error, the header's caption, and step details in run mode.
import { describe, expect, it } from "vitest";
import { edgeRun, tileRun } from "../src/app/editor/canvas/run-state";
import { layoutLtr } from "../src/app/editor/canvas/layout-ltr";
import { errorBannerLines } from "../src/app/editor/ndv/panes/error-banner";
import { RunData } from "../src/app/editor/runs-tab/run-data";
import {
  handledCaption,
  handledFailures,
  overlayOf,
  recordedLoops,
  stepStates,
} from "../src/app/editor/runs-tab/run-overlay";
import {
  readRunRecord,
  type RunRecord,
} from "../src/app/editor/runs-tab/run-record";
import type { Step, Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";

const RUN = "4e2a91c7-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
const ENV = "4a1b2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const NOW = "2026-10-07T12:00:00Z";
const REFUSED = {
  code: "REQUEST_REFUSED",
  message: "The service refused the request.",
  outcome: "failed",
  attempts: 3,
  instanceKey: "submit",
};

const action = (id: string, extra: object = {}): Step =>
  ({ id, kind: "action", uses: "requests.submit@1.0.0", ...extra }) as Step;
const transform = (id: string): Step =>
  ({ id, kind: "transform", value: { literal: {} } }) as Step;
/** lookup, then submit with an error path (notify-team), then summarize. */
const steps = (onError = "errorOutput"): Step[] => [
  action("lookup"),
  action("submit", {
    onError,
    ...(onError === "errorOutput"
      ? {
          errorPath: {
            steps: [action("notify-team")],
            output: { ref: "/steps/notify-team/output" },
          },
        }
      : {}),
  }),
  transform("summarize"),
];
const workflow = (list: Step[] = steps()): Workflow => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "request-intake", version: "1.3.0" },
  spec: { steps: list },
});

/** A runs.read answer, as the platform sends it. */
const record = (state: Record<string, unknown>): RunRecord =>
  readRunRecord({
    id: RUN,
    activation: {
      request: { version_id: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a" },
    },
    state: {
      status: "succeeded",
      active: [],
      input: { customerId: "c-1042" },
      steps: {},
      waits: {},
      branches: {},
      incidents: {},
      ...state,
    },
  })!;
/** The run: submit failed three times and its error path ran, then summarize. */
const handledRun = (error: Record<string, unknown> = REFUSED) =>
  record({
    steps: {
      lookup: { output: { rows: 1 } },
      submit: { output: { flagged: true }, error },
      summarize: { output: { ok: true } },
    },
  });

function fact(
  key: string,
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  const loop = [...key.matchAll(/\[(\d+)\]/g)].map((match) => Number(match[1]));
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: loop.length ? key : "",
    iteration: loop,
    kind: "action",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: null,
    child_run_id: null,
    log_entries: 0,
    ...extra,
  };
}
const handledFacts = (extra: Partial<StepFact> = {}): StepFact[] => [
  fact("lookup", "succeeded", { output: { rows: 1 } }),
  fact("submit", "failed", {
    error_code: "REQUEST_REFUSED",
    attempts: 3,
    handled: "errorOutput",
    ...extra,
  }),
  fact("notify-team", "succeeded", { output: { sent: true } }),
  fact("summarize", "succeeded", { kind: "transform" }),
];
const versions = (draft = workflow()) => ({
  run: draft,
  draft,
  runVersion: "1.3.0",
  test: false,
});

describe("a step whose failure its On error handled, on the Runs tab", () => {
  it("reads as handled from its step fact, above a finished instance and below a real failure", () => {
    expect(stepStates(handledRun(), handledFacts()).get("submit")).toBe(
      "handled",
    );
    const handled = fact("send[0]", "failed", {
      error_code: "REQUEST_REFUSED",
      handled: "continue",
    });
    // One item went on after its failure, the other finished: handled shows.
    expect(
      stepStates(record({}), [handled, fact("send[1]", "succeeded")]).get(
        "send",
      ),
    ).toBe("handled");
    // A real failure of another item still wins.
    expect(
      stepStates(record({ status: "failed" }), [
        handled,
        fact("send[1]", "failed", { error_code: "SMTP_REJECTED" }),
      ]).get("send"),
    ).toBe("failed");
  });

  it("reads as handled from the binding that holds its error, without step facts", () => {
    expect(stepStates(handledRun(), null).get("submit")).toBe("handled");
    expect(stepStates(handledRun(), null).get("lookup")).toBe("completed");
  });

  it("draws the handled tile, its failure and the way the run took into the error path", () => {
    const overlay = overlayOf(handledRun(), handledFacts(), versions());
    const canvas = overlay.canvas;
    expect(overlay.states.get("submit")).toBe("handled");
    expect(canvas.handled?.["submit"]).toEqual({
      code: "REQUEST_REFUSED",
      message: "The service refused the request.",
      policy: "errorOutput",
    });
    // Not where the run stopped: no failure, not current.
    expect(canvas.failures["submit"]).toBeUndefined();
    expect(canvas.current).not.toContain("submit");
    expect(tileRun("submit", canvas)).toBe("handled");
    const layout = layoutLtr(workflow(), {
      role: () => "app" as never,
      triggers: [{ id: "manual", kind: "manual" }],
    });
    const edges = Object.fromEntries(
      layout.edges.map((edge) => [edge.key, edgeRun(edge, canvas)]),
    );
    expect(edges).toMatchObject({
      "submit>submit:path:errorPath": "taken",
      "submit:path:errorPath>notify-team": "taken",
      "notify-team>$join:submit": "taken",
      "submit>submit:path:success": "skipped",
      "submit:path:success>$join:submit": "skipped",
    });
  });

  it("names the setting from the run's version when its step fact doesn't say", () => {
    const run = record({
      steps: {
        submit: { output: null, error: REFUSED },
      },
    });
    const continued = workflow(steps("continue"));
    const overlay = overlayOf(run, null, versions(continued));
    expect(overlay.canvas.handled?.["submit"]?.policy).toBe("continue");
  });

  it("marks a loop's item whose failure was handled with its code", () => {
    const loopFlow = workflow([
      {
        id: "orders",
        kind: "forEach",
        items: { literal: [1, 2, 3] },
        body: { steps: [action("send", { onError: "continue" })] },
      } as Step,
    ]);
    const [loop] = recordedLoops(
      loopFlow,
      record({ loops: { orders: { count: 3 } } }),
      [
        fact("send[0]", "succeeded"),
        fact("send[1]", "failed", {
          error_code: "REQUEST_REFUSED",
          handled: "continue",
        }),
        fact("send[2]", "succeeded"),
      ],
    );
    expect(loop.items[1]).toEqual({
      index: 1,
      status: "handled",
      code: "REQUEST_REFUSED",
    });
  });

  it("lists each handled failure once, with what the binding and the fact say", () => {
    expect(handledFailures(handledRun(), handledFacts())).toEqual([
      {
        key: "submit",
        step: "submit",
        code: "REQUEST_REFUSED",
        message: "The service refused the request.",
        outcome: "failed",
        attempts: 3,
        policy: "errorOutput",
      },
    ]);
    // Inside a loop that ended, the item's binding is gone: the fact alone.
    expect(
      handledFailures(record({}), [
        fact("send[1]", "failed", {
          error_code: "REQUEST_REFUSED",
          attempts: 2,
          handled: "continue",
        }),
      ]),
    ).toEqual([
      {
        key: "send[1]",
        step: "send",
        code: "REQUEST_REFUSED",
        message: "",
        outcome: null,
        attempts: 2,
        policy: "continue",
      },
    ]);
  });

  it("says in the header how many failures were handled and the first one", () => {
    const [first] = handledFailures(handledRun(), handledFacts());
    expect(handledCaption(1, first)).toBe(
      "1 error handled · submit: REQUEST_REFUSED",
    );
    expect(handledCaption(2, { ...first, key: "send[1]", step: "send" })).toBe(
      "2 errors handled · send, item 2: REQUEST_REFUSED",
    );
    expect(handledCaption(0, null)).toBe("");
  });
});

describe("step details in run mode", () => {
  it("reads a handled step as handled, with its failure", () => {
    const data = new RunData(handledRun(), ENV, workflow());
    data.setFacts(handledFacts());
    expect(data.state("submit")).toBe("handled");
    expect(data.failure("submit")).toBeNull();
    expect(data.handled("submit")).toEqual({
      code: "REQUEST_REFUSED",
      message: "The service refused the request.",
      outcome: "failed",
      attempts: 3,
      policy: "errorOutput",
    });
    expect(data.handled("lookup")).toBeNull();
  });

  it("the banner says the outcome in words, and only the attempts when the run doesn't say", () => {
    const lines = (outcome: string | null, attempts = 3) =>
      errorBannerLines({
        code: "REQUEST_REFUSED",
        message: "The service refused the request.",
        outcome,
        attempts,
        policy: "errorOutput",
      });
    expect(lines("failed")).toEqual({
      failed: "This step failed: REQUEST_REFUSED.",
      message: "The service refused the request.",
      outcome: "Outcome: Didn't happen. Attempts: 3.",
      setting: "The run continued (On error: Continue with error output).",
    });
    expect(lines("not_started").outcome).toBe(
      "Outcome: Didn't start. Attempts: 3.",
    );
    expect(lines("unknown").outcome).toBe("Outcome: Unknown. Attempts: 3.");
    expect(lines(null, 1500).outcome).toBe("Attempts: 1,500.");
  });

  it("shows any code as text and cuts it at 128 characters", () => {
    const long = "A".repeat(140);
    const data = new RunData(
      handledRun({ ...REFUSED, code: "request-refused" }),
      ENV,
      workflow(),
    );
    data.setFacts(handledFacts({ error_code: "request-refused" }));
    const failure = data.handled("submit")!;
    expect(errorBannerLines(failure).failed).toBe(
      "This step failed: request-refused.",
    );
    expect(
      overlayOf(
        handledRun({ ...REFUSED, code: "request-refused" }),
        handledFacts({ error_code: "request-refused" }),
        versions(),
      ).canvas.handled?.["submit"]?.code,
    ).toBe("request-refused");
    expect(errorBannerLines({ ...failure, code: long }).failed).toBe(
      `This step failed: ${"A".repeat(128)}.`,
    );
    const [first] = handledFailures(
      handledRun({ ...REFUSED, code: long }),
      handledFacts({ error_code: long }),
    );
    expect(handledCaption(1, first)).toBe(
      `1 error handled · submit: ${"A".repeat(128)}`,
    );
  });
});
