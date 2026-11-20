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
// A step whose failure its On error handled, in a simulated run: the
// canvas reads the trace's error and handled into a "handled" tile, which
// the run went past, and its badge says "Failed, continued" with the code
// as given, cut at 128 characters (run data is not trusted to be short).
import { describe, expect, it } from "vitest";
import {
  failureCodeText,
  handledStatus,
  runFact,
  tileBadge,
  type TileFacts,
} from "../src/app/editor/canvas/tile-facts";
import {
  edgeRun,
  tileRun,
  type CanvasRun,
} from "../src/app/editor/canvas/run-state";
import { layoutLtr } from "../src/app/editor/canvas/layout-ltr";
import { freshWorkflow, type Step } from "../src/app/model";
import { finalRun } from "../src/app/editor/canvas/trace-playback";
import type { ExecuteResponse } from "../src/app/editor/state/execution";

const ERROR = {
  code: "REQUEST_REFUSED",
  message: "The service refused the request.",
  outcome: "failed" as const,
  attempts: 1,
  instanceKey: "submit",
};
const response = {
  status: "completed",
  trace: [
    {
      index: 0,
      nodeId: "submit",
      instanceKey: "",
      kind: "action",
      status: "failed",
      error: ERROR,
      handled: "errorOutput",
    },
    {
      index: 1,
      nodeId: "notify-team",
      instanceKey: "",
      kind: "transform",
      status: "completed",
    },
  ],
  loops: [],
  blocked: null,
} as unknown as ExecuteResponse;
const facts = (extra: Partial<TileFacts> = {}): TileFacts => ({
  errors: [],
  warnings: [],
  setup: [],
  run: null,
  failure: null,
  stale: null,
  pinned: false,
  selected: false,
  ...extra,
});

describe("a handled failure in a simulated run", () => {
  it("is a handled tile the run went past, with its error and setting", () => {
    const run = finalRun(response);
    expect(run.status).toBe("succeeded");
    expect(run.current).toEqual([]);
    expect(tileRun("submit", run)).toBe("handled");
    expect(tileRun("notify-team", run)).toBe("done");
    expect(run.handled).toEqual({
      submit: {
        code: "REQUEST_REFUSED",
        message: "The service refused the request.",
        policy: "errorOutput",
      },
    });
  });

  it("badges the tile Failed, continued, after a failure and before a change", () => {
    const handled = facts({
      run: "handled",
      failure: {
        code: "REQUEST_REFUSED",
        message: "The service refused the request.",
      },
      handled: "errorOutput",
      changed: true,
    });
    expect(tileBadge(handled)).toEqual({
      kind: "handled",
      tone: "warning",
      icons: ["degraded"],
      text: "Failed: REQUEST_REFUSED, The service refused the request. The run continued (On error: Continue with error output).",
    });
    expect(
      tileBadge(
        facts({
          run: "handled",
          failure: { code: "TIMEOUT" },
          handled: "continue",
        }),
      )?.text,
    ).toBe("Failed: TIMEOUT. The run continued (On error: Continue).");
    expect(
      tileBadge(facts({ run: "failed", failure: { code: "X" } }))?.kind,
    ).toBe("failed");
  });

  it("shows a code as given, cut at 128 characters", () => {
    expect(failureCodeText("request-refused")).toBe("request-refused");
    expect(failureCodeText("A".repeat(140))).toBe("A".repeat(128));
  });
});

describe("a handled tile's lines", () => {
  it("shows the code alone in the warning tone, and says it failed and continued in its name", () => {
    const handled = facts({
      run: "handled",
      failure: { code: "REQUEST_REFUSED", message: "Refused." },
      handled: "errorOutput",
    });
    expect(runFact(handled)).toEqual({
      text: "REQUEST_REFUSED",
      tone: "warning",
    });
    expect(
      runFact(facts({ ...handled, failure: { code: "A".repeat(140) } })),
    ).toEqual({ text: "A".repeat(128), tone: "warning" });
    expect(handledStatus(handled)).toBe(
      "failed and continued, REQUEST_REFUSED",
    );
    expect(runFact(facts({ run: "done" }))).toBeNull();
    expect(handledStatus(facts({ run: "done" }))).toBe("");
  });
});

describe("the edges around a step whose error path ran", () => {
  const layout = () => {
    const workflow = freshWorkflow();
    const step = (id: string) => ({ id, kind: "transform" }) as Step;
    workflow.spec.steps = [
      {
        id: "submit",
        kind: "action",
        uses: "a@1.0.0",
        onError: "errorOutput",
        errorPath: { steps: [step("notify-team")], output: { literal: null } },
      } as Step,
      step("summarize"),
    ];
    return layoutLtr(workflow, {
      role: () => "app" as never,
      triggers: [{ id: "manual", kind: "manual" }],
    });
  };
  const states = (run: CanvasRun) =>
    Object.fromEntries(
      layout().edges.map((edge) => [edge.key, edgeRun(edge, run)]),
    );

  it("takes the Error output, the path and the way out of it, never the Success output", () => {
    const run = finalRun({
      ...response,
      trace: [
        ...response.trace,
        {
          index: 2,
          nodeId: "summarize",
          instanceKey: "",
          kind: "transform",
          status: "completed",
        },
      ],
    } as ExecuteResponse);
    expect(states(run)).toMatchObject({
      "submit>submit:path:success": "skipped",
      "submit:path:success>$join:submit": "skipped",
      "submit>submit:path:errorPath": "taken",
      "submit:path:errorPath>notify-team": "taken",
      "notify-team>$join:submit": "taken",
      "submit>summarize": "taken",
    });
  });

  it("takes the Success output when the step succeeded, and not the error path", () => {
    const run = finalRun({
      status: "completed",
      trace: ["submit", "summarize"].map((nodeId, index) => ({
        index,
        nodeId,
        instanceKey: "",
        kind: "transform",
        status: "completed",
      })),
      loops: [],
      blocked: null,
    } as unknown as ExecuteResponse);
    expect(states(run)).toMatchObject({
      "submit>submit:path:success": "taken",
      "submit:path:success>$join:submit": "taken",
      "submit>submit:path:errorPath": "skipped",
      "notify-team>$join:submit": "skipped",
    });
  });
});
