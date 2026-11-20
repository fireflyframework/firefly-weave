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
  tileBadge,
  type TileFacts,
} from "../src/app/editor/canvas/tile-facts";
import { tileRun } from "../src/app/editor/canvas/run-state";
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
