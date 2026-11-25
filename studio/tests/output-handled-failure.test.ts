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
// A step that failed and continued in the last execution: its result keeps
// the failure and the setting that handled it (Continue's output is null),
// read back only in that shape, and its Output banner says what happened
// first: the code and message, the outcome and attempts, and the setting.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  readResults,
  withExecution,
} from "../src/app/editor/state/last-results";
import { errorBannerLines } from "../src/app/editor/ndv/panes/error-banner";
import { print } from "./execution-fixtures";

const ERROR = {
  code: "REQUEST_REFUSED",
  message: "The service refused the request.",
  outcome: "failed" as const,
  attempts: 1,
  instanceKey: "submit",
};
const response = (handled: "continue" | "errorOutput", output?: unknown) =>
  ({
    status: "completed",
    trace: [
      {
        index: 0,
        nodeId: "submit",
        instanceKey: "",
        kind: "action",
        status: "failed",
        error: ERROR,
        handled,
        ...(output !== undefined ? { output } : {}),
      },
    ],
    loops: [],
    blocked: null,
  }) as unknown as ExecuteResponse;
const prints = new Map([["submit", print(1)]]);

describe("a handled failure in the last results", () => {
  it("keeps the failure and its setting, with the error path's result as the output", () => {
    const results = withExecution(
      null,
      response("errorOutput", { flagged: true }),
      prints,
      "a",
    );
    expect(results.steps["submit"]).toMatchObject({
      status: "failed",
      output: { flagged: true },
      handled: {
        code: "REQUEST_REFUSED",
        message: "The service refused the request.",
        outcome: "failed",
        attempts: 1,
        policy: "errorOutput",
      },
    });
    // Continue: the step's output is null.
    expect(
      withExecution(null, response("continue"), prints, "a").steps["submit"]
        .output,
    ).toBeNull();
  });

  it("reads a kept failure back only with its code and setting", () => {
    const kept = withExecution(null, response("continue"), prints, "a");
    expect(readResults(JSON.stringify(kept))?.steps["submit"].handled).toEqual(
      kept.steps["submit"].handled,
    );
    const broken = {
      ...kept,
      steps: { submit: { ...kept.steps["submit"], handled: { policy: "x" } } },
    };
    expect(readResults(JSON.stringify(broken))).toBeNull();
  });
});

describe("the banner of a step that failed and continued", () => {
  it("says the code and message, the outcome and attempts, then the setting", () => {
    expect(errorBannerLines({ ...ERROR, policy: "errorOutput" })).toEqual({
      failed: "This step failed: REQUEST_REFUSED.",
      message: "The service refused the request.",
      outcome: "Outcome: Didn't happen. Attempts: 1.",
      setting: "The run continued (On error: Continue with error output).",
    });
    const lines = errorBannerLines({
      ...ERROR,
      code: "A".repeat(140),
      outcome: "unknown",
      attempts: 3,
      policy: "continue",
    });
    expect(lines.failed).toBe(`This step failed: ${"A".repeat(128)}.`);
    expect(lines.outcome).toBe("Outcome: Unknown. Attempts: 3.");
    expect(lines.setting).toBe("The run continued (On error: Continue).");
    expect(
      errorBannerLines({ ...ERROR, outcome: "not_started", policy: "continue" })
        .outcome,
    ).toBe("Outcome: Didn't start. Attempts: 1.");
  });

  it("says the code once when the message only repeats it", () => {
    const lines = errorBannerLines({
      ...ERROR,
      message: "The step failed with code REQUEST_REFUSED.",
      policy: "errorOutput",
    });
    expect(lines.failed).toBe("This step failed: REQUEST_REFUSED.");
    expect(lines.message).toBe("");
  });
});
